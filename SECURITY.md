# Segurança

Este é um projeto experimental, mantido por uma pessoa, sem revisão
profissional de segurança. Relatos são muito bem-vindos.

## Como relatar uma vulnerabilidade

**Não abra uma issue pública** para um problema de segurança. Use o relato
privado do GitHub:

1. Abra a aba **Security** do repositório.
2. Clique em **Report a vulnerability** (em "Security advisories").
3. Descreva o problema, como reproduzi-lo e o impacto que você vê.

O relato fica visível só para você e para o mantenedor. Não há prazo de
resposta garantido.

## Nunca envie dados reais

Não inclua em relatos, issues ou pull requests:

- conteúdo de conversas, planilhas ou e-mails;
- arquivos de snapshot (`.leveldb`, `.blob`), o arquivo `conversas.sqlite`,
  pacotes de restauração ou exportações;
- tokens, ou qualquer arquivo da pasta de dados.

Se precisar mostrar um caso, reproduza-o com dados sintéticos (o gerador em
`tests/idb_builder.py` serve para isso).

## O que mais interessa revisar

- **Snapshots brutos.** Contêm tokens de conectores. A ferramenta os guarda em
  uma pasta com permissão restrita ao usuário (`icacls`) e nunca os decodifica.
  Qualquer caminho pelo qual esse conteúdo possa vazar para exportações, logs
  ou relatórios é uma vulnerabilidade.
- **Restauração.** `restore-snapshot --execute` é o único comando que escreve
  na pasta do Office. Falhas nas travas (Office aberto, banco em uso, esquema
  diferente, confirmação) ou no desfazer automático são graves.
- **Script de restauração seletiva** (`restore_script.js`). Roda dentro da
  página do suplemento, que está autenticada. Ele não pode acessar a rede, nem
  alterar ou apagar registros existentes.
- **Leitura do IndexedDB/LevelDB.** A ferramenta interpreta arquivos binários;
  um arquivo malformado não pode causar mais do que uma falha de leitura.
- **Filtro de credenciais das exportações.** É uma rede de segurança baseada em
  padrões, e certamente incompleta.

## O que a ferramenta promete

- Não acessa a rede e não tem telemetria.
- Fora da restauração manual, não escreve na pasta do Office.
- Os logs não contêm texto de conversa.

Se você encontrar um caso em que alguma dessas promessas não vale, isso é uma
vulnerabilidade: por favor, relate.

## Versões

Só a versão mais recente recebe correções.
