# Como contribuir

Obrigado pelo interesse! Este é um projeto pequeno, experimental e mantido por
uma pessoa nas horas vagas. Toda ajuda é bem-vinda: relato de erro, teste em
outra máquina, revisão de código, correção ou melhoria de documentação.

*English speakers are welcome: issues and pull requests may be written in
English. Code comments and documentation are in Portuguese.*

## Antes de tudo: de onde este código veio

O código foi escrito por **vibe coding**: gerado com ajuda de IA (Claude Code)
e guiado por um analista de dados, não por um desenvolvedor profissional. Ele
tem testes automatizados, mas **não passou por revisão profissional**.

Na prática, isso significa que:

- desconfiar do código é saudável, e apontar um defeito é uma contribuição;
- uma revisão de quem conhece o assunto vale tanto quanto um pull request;
- contribuições escritas com ajuda de IA são aceitas, desde que você tenha
  lido, entendido e testado o que está enviando.

## A regra que não tem exceção: nenhum dado real

As conversas guardadas pela ferramenta contêm dados de trabalho, e os snapshots
brutos contêm tokens de conectores. Por isso, **nunca** coloque em issues,
discussões, pull requests, testes ou exemplos:

- texto de conversas, planilhas ou e-mails reais;
- arquivos de snapshot (`.leveldb`, `.blob`), `conversas.sqlite`, exportações,
  relatórios ou pacotes de restauração;
- tokens, ids reais de conversa, de usuário ou de organização;
- logs ou caminhos com o seu nome de usuário.

Para mostrar um problema, reproduza-o com dados sintéticos. O gerador
`tests/idb_builder.py` cria um armazenamento falso no mesmo formato do real.

## Formas de ajudar

**Relatar um erro.** Abra uma issue com o modelo "Relato de erro". Diga a
versão (`office-chat-keeper --version`), o Windows, o Office, o app (Excel,
Word, PowerPoint, Outlook) e o que aconteceu. Mensagens de erro podem ser
coladas depois de conferir que não têm conteúdo de conversa.

**Testar em um ambiente diferente.** A ferramenta foi testada em uma única
máquina, com uma conta, no Excel. Contar que funcionou (ou não) no Word, no
PowerPoint, no Outlook ou em outra versão do Office já ajuda muito.

**Revisar o código.** As partes que mais precisam de olhos experientes estão
listadas no [SECURITY.md](SECURITY.md) e no [ROADMAP.md](ROADMAP.md). Para um
problema de segurança, **não abra issue pública**: siga o SECURITY.md.

**Sugerir uma melhoria.** Use o modelo "Sugestão de melhoria". Para dúvidas e
ideias ainda soltas, prefira as Discussões do repositório.

**Enviar código.** Veja abaixo.

## Enviar um pull request

1. Para mudanças maiores, abra antes uma issue e combine a ideia. Isso evita
   trabalho perdido.
2. Faça um *fork* (a sua cópia do repositório no GitHub) e crie uma *branch*
   (uma linha de trabalho separada) a partir da `main`.
3. Prepare o ambiente e rode os testes como descrito em
   [docs/desenvolvimento.md](docs/desenvolvimento.md):

   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -e ".[dev]"
   .venv\Scripts\python -m pytest
   ```

4. Rode a verificação de dados sensíveis antes de cada commit:

   ```
   .venv\Scripts\python tools\verificar_antes_de_publicar.py
   ```

5. Abra o pull request e preencha o modelo. O GitHub Actions roda os testes em
   Windows, com Python 3.11, 3.12 e 3.13.

## O que se espera de uma mudança

- **Testes.** Toda correção ou função nova vem com teste, usando apenas
  fixtures sintéticas criadas por código.
- **Sem rede.** A ferramenta é 100% local. Nenhuma mudança pode acessar a rede
  ou enviar dados a qualquer lugar.
- **Somente leitura na origem.** Só o módulo `restore.py` escreve na pasta do
  Office, e só por comando manual com confirmação. Isso não muda.
- **Sem dependências novas.** O programa usa só a biblioteca padrão do Python.
  Uma dependência nova precisa de um motivo muito bom e de discussão prévia.
- **Logs sem conteúdo.** Só contagens, ids e nomes de pasta.
- **Comentários e documentação em português**, explicando o porquê e os
  conceitos. Parte do público não é de desenvolvedores.
- **Mudanças pequenas e focadas.** Um assunto por pull request.
- **Código de terceiros** (`src/office_chat_keeper/_vendor`) só é alterado
  quando inevitável, e a alteração é anotada no `_vendor/README.md`.

## Etiquetas das issues

| Etiqueta | Significado |
|---|---|
| `good first issue` | Boa para quem está chegando: pequena e bem delimitada. |
| `help wanted` | O mantenedor precisa de ajuda (em geral, de outro ambiente ou de mais experiência). |
| `security` | Assunto de segurança que pode ser discutido em público. |
| `needs-review` | Pede a revisão de alguém com experiência na área. |

## Revisão e prazos

O mantenedor responde quando pode; não há prazo garantido. Como ele não é
desenvolvedor profissional, pull requests fáceis de entender (pequenos, com
testes e com a explicação do porquê) são aceitos mais depressa.

## Conduta e licença

- Ao participar, você concorda com o
  [Código de Conduta](CODE_OF_CONDUCT.md).
- Ao contribuir, você concorda que a sua contribuição seja distribuída sob a
  licença [MIT](LICENSE) do projeto.
