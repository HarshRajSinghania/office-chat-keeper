# Como funciona

Explicação de cada parte da ferramenta, com os conceitos por trás. Escrita para quem conhece análise de dados, mas não necessariamente LevelDB, IndexedDB ou o Agendador de Tarefas.

```
origem (Office) --cópia--> snapshot --leitura--> arquivo permanente --> exportação
  só leitura               foto bruta            memória (só cresce)    Markdown/JSON
```

## O problema

Os suplementos do Claude guardam o histórico **só no seu computador**. Depois de
uma atualização, o Office pode abrir o suplemento em uma pasta de armazenamento
nova e vazia, e o histórico antigo some. Não existe cópia em nenhum servidor.
O backup precisa existir **antes** da perda: esta ferramenta não recupera o que
já se perdeu.

## Conceitos

**WebView2.** O painel do Claude dentro do Excel é uma página web
(`https://pivot.claude.ai`) rodando em um navegador embutido, o WebView2, que é
o mesmo motor do Edge/Chrome (Chromium).

**IndexedDB.** É o banco de dados que os navegadores oferecem às páginas. Cada
site ("origem") tem o seu, isolado dos outros. O suplemento grava as conversas
lá.

**LevelDB.** É o formato em que o Chromium guarda o IndexedDB no disco: um banco
chave-valor feito de vários arquivos em uma pasta.

| Arquivo | Papel |
|---|---|
| `000006.log` | Diário: toda gravação nova é anexada aqui primeiro. |
| `000008.ldb` | Tabelas ordenadas e **imutáveis** (nunca editadas, só criadas e apagadas). |
| `MANIFEST-000001` | Índice de quais `.ldb` valem agora. |
| `CURRENT` | Diz qual MANIFEST está em uso. |
| `LOCK` | Arquivo vazio que o Office mantém travado enquanto usa o banco. |
| `LOG`, `LOG.old` | Texto de diagnóstico, sem dados. |

Valores grandes (uma conversa com imagens pode ter dezenas de MB) não cabem bem
no LevelDB e vão para arquivos separados na pasta irmã `.blob`.

**Onde fica.**

```
%LOCALAPPDATA%\Microsoft\Office\16.0\Wef\webview2\<id>_ADAL\2\<n>\EBWebView\Default\IndexedDB\
    https_pivot.claude.ai_0.indexeddb.leveldb
    https_pivot.claude.ai_0.indexeddb.blob
```

O `<n>` muda (`2\0`, `2\1`...). Por isso a ferramenta **procura** em todo o
`Wef\webview2` em vez de fixar um caminho. Cada pasta encontrada é uma *fonte*.

## Descoberta e snapshot

### 1. Descoberta (`discovery.py`)

Percorre `Wef\webview2` e lista toda pasta chamada
`https_pivot.claude.ai_0.indexeddb.leveldb`. Para cada uma anota a data de
criação do arquivo `LOCK`, que funciona como "certidão de nascimento" do banco:
se essa data mudar, o banco foi recriado.

### 2. Snapshot (`snapshot.py`)

Um snapshot é uma fotografia das pastas `.leveldb` e `.blob`.

1. **Só copia se mudou.** Calcula uma impressão digital (nome, tamanho e data de
   cada arquivo de dados). Igual à do último snapshot? Não faz nada.
2. **Funciona com o Excel aberto.** Abre os arquivos apenas para leitura. Se um
   arquivo estiver bloqueado, espera e tenta de novo.
3. **Garante uma cópia coerente.** Tira a impressão digital antes e depois de
   copiar. Se o Office gravou no meio, descarta e refaz. Se a fonte não parar de
   mudar, guarda a cópia marcada como `stable: false`.
4. **Economiza disco com hardlinks.** Arquivo idêntico ao do snapshot anterior
   não é copiado de novo: recebe um *hardlink*, isto é, um segundo nome para os
   mesmos bytes no disco. Um blob de 50 MB ocupa 50 MB uma única vez.
5. **Nunca deixa snapshot pela metade.** Copia para uma pasta `.incompleto-...`
   e só no final a renomeia para `AAAAMMDD-HHMMSS`.
6. **Grava um `manifest.json`** com a lista de arquivos, tamanhos e SHA-256.

### 3. Retenção

Ficam sempre:

- os 10 snapshots mais recentes;
- o mais recente de cada dia, nos últimos 30 dias;
- o mais recente de cada "geração" do banco (mesma data de criação). É a regra
  que protege contra a perda: se o Office recriar o banco vazio, a última foto
  do banco antigo **nunca** é apagada.

### 4. Proteção da pasta (`protect.py`)

Na primeira execução, a pasta de dados é restrita ao seu usuário com o `icacls`
(remove as permissões herdadas e concede acesso só à sua conta). Motivo: o
snapshot bruto contém **tokens de conectores**.

## Leitura e exportação

### 1. Leitura (`reader.py`)

Ler uma conversa é descascar quatro camadas:

1. **LevelDB**: pares chave-valor em bytes. Regravar uma chave não apaga a
   versão antiga na hora, então podem existir várias versões; vale a de maior
   número de sequência.
2. **IndexedDB**: cada chave começa com um prefixo que diz a qual banco e a qual
   *object store* (a "tabela") ela pertence.
3. **V8 Structured Clone**: o valor é um objeto JavaScript serializado, uma
   sequência de marcas (`o` objeto, `"` texto, `N` número, `D` data...).
4. **Blob externo**: valores grandes ficam em um arquivo da pasta `.blob`, e o
   registro guarda só um ponteiro.

Essas camadas são decodificadas pela biblioteca open source
[`ccl_chromium_reader`](https://github.com/cclgroupltd/ccl_chromium_reader)
(CCL Solutions, licença MIT). Os módulos necessários da versão 0.3.19 vão
**embutidos** no pacote (pasta `src/office_chat_keeper/_vendor`, com as
licenças e a lista do que foi alterado), então não há nada a instalar à parte.
A leitura é sempre feita sobre um **snapshot**, nunca sobre a pasta viva do
Office.

### 2. O formato real das conversas (`conversation.py`)

Confirmado em um armazenamento real de novembro de 2026:

| Banco | Store | Conteúdo |
|---|---|---|
| `claude-chat-history` | `chats` | Uma conversa por registro (chave = id). |
| `claude-chat-history` | `blobs` | Dados auxiliares da conversa (`chatId`, `name`, `bytes`). |
| `claude-local-skills` | `skills` | Skills enviadas. |
| `claude-mcp-gateways` | `gateways` | Tokens de conectores. **Nunca é lido.** |
| `claude-office-snipped-results`, `claude-office-inflight` | | Estado interno; ficam só no snapshot bruto. |

Cada conversa traz `id`, `userId`, `orgId`, `fileId`, `surface` (o app; `sheet`
é o Excel), `title`, `createdAt`, `updatedAt` e um campo `snapshot` com três
visões da mesma conversa:

- `snapshot.ui.messages`: o que aparece na tela. É o que exportamos. Cada
  mensagem tem `uuid`, `timestamp` e `type` (`user`, `assistant`, `thinking` ou
  `tool`). As de ferramenta trazem `name`, `status`, `input`, `result` e as
  imagens em base64.
- `snapshot.api.transcript`: o que foi enviado ao modelo. Usado só como reserva,
  se `ui.messages` não existir.
- `snapshot.agent`: estado interno (resumos de contexto).

Esse formato é interno e pode mudar. O código trata campo ausente ou de tipo
inesperado como vazio, em vez de falhar, e avisa quando `snapshot.version` não
é a versão testada (1).

### 3. Exportação (`export.py`)

```
exports\conversas\<app>\<ano>\<AAAA-MM-DD>_<titulo>_<id8>\
    conversa.md      para ler
    conversa.json    tudo estruturado
    imagens\001-ab12cd34.png
    anexos\<nome>.json
exports\skills\...   exports\memoria\...
```

- **Markdown enxuto.** As mensagens do usuário e do Claude aparecem inteiras. O
  que acontece entre elas vira um bloco "Atividade do Claude", com uma linha por
  ferramenta (nome e explicação curta). Código e resultados grandes ficam só no
  JSON. Os raciocínios entram no Markdown apenas com `--include-thinking`.
- **Imagens como arquivos.** O base64 é decodificado e salvo em `imagens\`.
- **Sem tokens.** Além de o banco de conectores não ser lido, todo texto
  exportado passa por um filtro que troca padrões com cara de credencial
  (chaves `sk-ant-...`, JWT, `Bearer ...`) por um aviso. É uma rede de
  segurança, não uma garantia: revise antes de compartilhar uma exportação.
- **Nunca piora uma exportação.** Se a conversa lida tiver menos mensagens que
  a já exportada, os arquivos antigos são mantidos. Conversas que somem da
  origem nunca são apagadas da exportação.

Na conversa real usada no teste, 48 MB de armazenamento bruto viraram um
Markdown de 110 KB, um JSON de 3,4 MB e 43 imagens.

### Limitações

- Testado com **uma** conversa real, do Excel. Os valores de `surface` dos
  outros apps (Word, PowerPoint, Outlook) são suposições; se não baterem, o
  valor original aparece como está.
- `claude-local-skills` estava vazio e `claude-memory` não existia no
  armazenamento de teste. A exportação desses bancos é **genérica** (grava cada
  registro em JSON e extrai campos de texto e binários) e só foi testada com
  dados sintéticos.

## Arquivo permanente e alertas

```
origem (Office) --cópia--> snapshot --leitura--> arquivo permanente --> exportação
  só leitura               foto bruta            memória (só cresce)    Markdown/JSON
```

O snapshot é uma **foto**: se o Office recriar o banco vazio, a próxima foto sai
vazia. O arquivo permanente (`archive\conversas.sqlite`) é a **memória**: tudo o
que já apareceu em alguma foto fica guardado, por id de conversa. A exportação
passa a ler do arquivo, então uma fonte vazia nunca esvazia o que já foi
exportado.

### As regras do arquivo (`archive.py`)

| Regra | O que garante |
|---|---|
| **A. União** | Toda mensagem nova é acrescentada. Mensagem que some da versão atual é só *marcada* como ausente, nunca apagada. |
| **B. Versão suspeita não vira a atual** | Se a conversa chegar vazia, ou sem mais da metade das mensagens arquivadas, o arquivo fica como está e um alerta é gravado. |
| **C. Versão antiga não rebaixa a atual** | Se o `updatedAt` que chegou é anterior ao arquivado, só o que for inédito é aproveitado. |
| **D. Histórico** | Cada mudança aceita vira uma linha em `conversation_versions`. O texto anterior de uma mensagem alterada vai para `message_versions`. |
| **E. Trava no banco** | Gatilhos (*triggers*) do SQLite recusam qualquer `DELETE` nas tabelas de conteúdo. Nem um bug neste programa conseguiria apagar o histórico. |

Conceitos usados:

- **Transação.** Cada snapshot entra no arquivo inteiro ou não entra: se algo
  falhar no meio, o SQLite desfaz tudo.
- **Identidade por `uuid`.** Cada mensagem tem um `uuid`; é por ele que o
  arquivo sabe se uma mensagem é nova, igual ou alterada. As versões guardam só
  a diferença, não uma cópia inteira da conversa.
- **Conteúdo endereçado por hash.** Imagens e anexos ficam na tabela `blobs`,
  uma vez por SHA-256, mesmo que apareçam em várias mensagens.

Mensagens ausentes aparecem na exportação em uma seção própria, no fim do
Markdown ("Mensagens que não estão mais na versão atual").

### Alertas de possível perda

| Alerta | Quando aparece |
|---|---|
| `source_smaller_than_archive` | Uma fonte nunca vista não tem a maior parte das conversas do arquivo (o caso da pasta nova e vazia). |
| `conversations_missing` | Conversas que estavam em uma fonte deixaram de estar nela. |
| `database_recreated` | A data de criação do banco de uma fonte mudou. |
| `conversation_shrunk` | Uma conversa chegou vazia ou muito menor (regra B). |
| `no_source_found` | Nenhuma fonte no disco, mas o arquivo tem conversas. |

Nenhum alerta apaga ou altera nada: ele só registra e avisa. O mesmo problema
não gera alertas repetidos a cada execução, e os alertas de ausência se
resolvem sozinhos se a conversa reaparecer. Os alertas aparecem no `run` e no
`status`. A verificação de ausência é pulada quando o snapshot é instável ou
tem registros ilegíveis, para uma falha de leitura não parecer uma perda.

O arquivo guarda o conteúdo fiel, sem o filtro de credenciais (que é aplicado
na exportação). Por isso fica na pasta de dados protegida.

Um alerta fica aberto até o problema se resolver ou até você marcá-lo como
visto (`alerts --ack`). Marcar como visto não apaga o alerta: ele continua no
histórico (`alerts --all`).

## Busca

Procurar um termo lendo todas as mensagens uma a uma ficaria lento. A busca usa
um **índice de texto completo** (o FTS5, que já vem no SQLite): uma tabela que
guarda, para cada palavra, em quais mensagens ela aparece, como o índice
remissivo no fim de um livro.

- **O que é indexado:** o texto das mensagens do usuário e do Claude, os
  raciocínios, o título das conversas e, nas chamadas de ferramenta, o nome, a
  explicação, o código, o erro e os dados do resultado.
- **Sem acento e sem maiúscula:** `conciliacao` acha "Conciliação".
- **Várias palavras:** todas precisam estar na mesma mensagem, em qualquer
  ordem. Com `--phrase`, a frase exata. Com `*` no fim, por prefixo
  (`concilia*`).
- **Ordem dos resultados:** títulos, depois mensagens suas e do Claude, depois
  raciocínios e, por último, chamadas de ferramenta. Dentro de cada grupo, por
  relevância.
- **Mensagens ausentes continuam pesquisáveis** e aparecem marcadas como
  "só no arquivo".
- **O índice é derivado.** Ele é atualizado a cada ingestão e, se sair de
  sincronia ou o arquivo for de uma versão anterior, é reconstruído sozinho ao
  abrir. Só a versão atual de cada mensagem é indexada; textos anteriores de
  uma mensagem editada ficam guardados, mas fora da busca.

O que você digita é tratado como texto literal, então aspas, dois-pontos e
palavras como `AND` não quebram a busca.

## Backup automático

```
office-chat-keeper install-task               (liga; padrão: a cada 15 minutos)
office-chat-keeper install-task --interval 30
office-chat-keeper uninstall-task             (desliga; não apaga nenhum dado)
```

O **Agendador de Tarefas** é o serviço do Windows que executa programas em
horários ou eventos definidos. Uma tarefa tem quatro partes:

| Parte | O que define | O que usamos |
|---|---|---|
| Gatilhos | Quando rodar | No logon (com 1 minuto de folga) e a cada N minutos |
| Ação | O que rodar | `pythonw.exe -P -m office_chat_keeper --data-dir ... run` |
| Principal | Com qual conta | A sua, sem elevação, só com você logado |
| Configurações | Como rodar | Prioridade baixa, uma execução por vez, limite de 30 minutos |

- **Não exige administrador nem guarda senha.** A tarefa é da sua conta e só
  roda com você logado.
- **Sem janela.** A ação usa `pythonw.exe`, a versão do Python sem console.
- **Pouca CPU.** Prioridade 7 (abaixo do normal, o padrão do Agendador), e o
  `run` só copia e lê quando algo mudou. Sem mudança, termina em instantes.
- **Uma execução por vez.** O Windows pula um disparo se o anterior ainda
  estiver rodando, e o próprio `run` usa um arquivo de trava
  (`state\run.lock`): se você rodar à mão enquanto a tarefa roda, a segunda
  execução desiste.
- **Erros vão para o log.** Rodando sem janela não há onde mostrar um erro,
  então ele é gravado em `logs\office-chat-keeper.log` (tipo e local do erro,
  sem a mensagem). O `status` mostra a última execução e o resultado.

A tarefa é criada com o `schtasks.exe`, ferramenta do próprio Windows, a partir
de uma definição em XML que fica salva em `state\task.xml` para você conferir.

**Por que não um script PowerShell.** Em muitas empresas a política de execução
do PowerShell (`AllSigned`) só permite scripts assinados pela TI. Um script
desta ferramenta seria bloqueado, e contornar a política não é opção. O
`schtasks.exe` é um executável do Windows, não um script, e cria a mesma
tarefa.

**Se a criação for recusada.** Se o Windows não aceitar o gatilho de logon, a
ferramenta cria a tarefa só com o intervalo e avisa. Se recusar a tarefa
inteira (acesso negado), a política da empresa pode impedir tarefas agendadas
por usuário; nesse caso, peça orientação à TI.

A tarefa aponta para o Python em que a ferramenta está instalada. Se você
reinstalar em outro lugar (outro ambiente virtual, por exemplo), rode
`install-task` de novo.

### Alternativa: scripts PowerShell

A pasta `scripts\` traz dois scripts que criam e removem a mesma tarefa usando
os cmdlets do PowerShell (`Register-ScheduledTask` e companhia), para quem
prefere esse caminho ou quer aprender como a tarefa é montada:

```
powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1
powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -IntervalMinutes 30
powershell -ExecutionPolicy Bypass -File scripts\uninstall-task.ps1
```

Parâmetros do `install-task.ps1`: `-IntervalMinutes` (padrão 15), `-TaskName`,
`-DataDir`, `-PythonPath` (o Python em que o pacote está instalado) e `-NoRun`.

- Use os scripts **ou** o comando `office-chat-keeper install-task`, não os
  dois: eles criam a tarefa com o mesmo nome e um substitui o outro.
- `-ExecutionPolicy Bypass` vale só para aquele processo. O Windows vem com a
  política `Restricted`, que não roda nenhum script. Se a política for imposta
  pela empresa (`AllSigned`, por exemplo), o script é bloqueado mesmo com esse
  parâmetro; nesse caso use `office-chat-keeper install-task`.
- **Estes scripts não foram executados no desenvolvimento**, justamente porque
  a máquina usada tinha a política `AllSigned`. Foram conferidos só por análise
  estática (sintaxe e nomes de parâmetros). Na primeira vez, teste com
  `-TaskName office-chat-keeper-teste -NoRun` e confira no Agendador de Tarefas
  antes de usar para valer.

## Verificação periódica

A cada `run` (e no comando `verify`), a ferramenta compara as fontes com o
arquivo permanente. Esta etapa **só lê e relata**: não toca no armazenamento do
Office e não restaura nada.

O que ela verifica:

| Pergunta | Como responde |
|---|---|
| Há fonte nova, ou alguma sumiu? | Compara as pastas encontradas agora com as que já foram fotografadas. Uma que some junto com outra que aparece é relatada como "possível mudança de pasta". |
| O banco foi recriado? | Compara a data de criação do banco entre os snapshots. |
| Há conversas do arquivo fora das fontes atuais? | Lista cada uma por id, título e app. |
| Há alerta ainda não visto? | Se houver, o resultado é "possível perda de histórico". |

O que ela produz, na pasta `reports\`:

- `ultima-verificacao.md` e `.json`: o retrato mais recente, regravado a cada
  execução.
- `perda-<data>.md`: cópia datada do relatório, gravada quando aparece uma
  perda nova.
- `plano-restauracao-<data>.md`: uma **simulação**. Descreve o que falta, onde
  restaurar, o que cada estratégia faria e o que se perderia, e aponta o
  Markdown exportado como caminho seguro. Não executa nada.

Junto com o relatório de perda sai uma **notificação do Windows**. Relatório
datado, plano e notificação saem uma vez por conjunto de alertas, não a cada
15 minutos. Depois que você marca os alertas como vistos (`alerts --ack`), o
resultado volta a "sem sinais de perda", mas as conversas ausentes continuam
listadas (e guardadas).

A notificação é feita em Python puro, chamando funções do próprio Windows, sem
PowerShell e sem rede. Ela é um extra: se as notificações estiverem desligadas
ou em "Não incomodar", não aparece. O alerta continua no `status`, no `alerts`
e no relatório. Para testar: `office-chat-keeper verify --test-notification`.

**Retenção e restauração.** Uma restauração fiel precisa do registro bruto, que
só existe nos snapshots. Por isso a retenção ganhou mais uma regra: nunca apaga
o snapshot em que uma conversa foi vista pela última vez. Para conversas ainda
presentes esse é sempre o snapshot mais novo; só as que sumiram "seguram" um
snapshot antigo.
