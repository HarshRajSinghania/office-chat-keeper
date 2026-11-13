/*
 * office-chat-keeper - restauração seletiva (estratégia B)
 * Ferramenta NÃO oficial. Não tem vínculo com a Anthropic nem com a Microsoft.
 *
 * O QUE ESTE SCRIPT FAZ
 *   Cole-o no Console das ferramentas de desenvolvedor (Ctrl+Shift+I) da página
 *   onde o banco "claude-chat-history" existe. Ele abre um pequeno painel, você
 *   escolhe (pelo botão do painel) o arquivo "dados-restauracao.json", e
 *   ele grava no IndexedDB SOMENTE as conversas que ainda não existem lá.
 *
 * O QUE ELE NÃO FAZ
 *   - Não acessa a rede (não há fetch, XMLHttpRequest, WebSocket nem envio de dados).
 *   - Não apaga nem altera conversas existentes: usa add(), que FALHA se a chave
 *     já existir, em vez de put(), que sobrescreveria.
 *   - Não cria o banco nem muda o esquema: se o banco não existir, ou se o
 *     esquema for diferente do esperado, ele para sem gravar nada.
 *   - Não grava nada antes de você ler a simulação e digitar GRAVAR.
 *
 * COMO ELE SE PROTEGE
 *   1. Confere o esquema vivo (versão, stores, chaves e índices) contra o do arquivo.
 *   2. Mostra a simulação: o que seria inserido, o que seria pulado e se
 *      userId/orgId seriam ajustados.
 *   3. Grava tudo em UMA transação: ou entra tudo, ou não entra nada.
 *   4. Relê cada registro gravado e compara com o arquivo. Se algo não conferir,
 *      remove apenas o que ele mesmo inseriu.
 *
 * Conceitos: IndexedDB é o banco de dados do navegador. Um "object store" é como
 * uma tabela; "keyPath" é o campo que serve de chave; uma "transação" agrupa
 * gravações de modo que valha tudo ou nada.
 */
(() => {
  'use strict';

  const DB_NAME = 'claude-chat-history';
  const CHATS = 'chats';
  const BLOBS = 'blobs';
  const SUPPORTED_FORMAT = 1;
  const CONFIRM_WORD = 'GRAVAR';
  const TYPED = {
    Int8Array, Uint8Array, Uint8ClampedArray, Int16Array, Uint16Array, Int32Array,
    Uint32Array, Float32Array, Float64Array, BigInt64Array, BigUint64Array,
  };

  // Se o script for colado duas vezes, fecha o painel anterior.
  if (window.__officeChatKeeperRestore && window.__officeChatKeeperRestore.close) {
    window.__officeChatKeeperRestore.close();
  }

  // ---------------------------------------------------------------- utilidades
  const request = (r) => new Promise((resolve, reject) => {
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });

  const finished = (tx) => new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(tx.error || new Error('a transação foi abortada'));
  });

  function setField(target, key, value) {
    // defineProperty: uma chave chamada "__proto__" vira campo comum, não protótipo.
    Object.defineProperty(target, key, {
      value, enumerable: true, writable: true, configurable: true,
    });
  }

  function base64ToBytes(text) {
    const binary = atob(text);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  function bytesToBase64(bytes) {
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  }

  // ------------------------------------- arquivo de dados <-> valores JavaScript
  // JSON só conhece texto, número, booleano, null, lista e objeto. O que o
  // JavaScript tem a mais (undefined, Date, ArrayBuffer...) vem no arquivo como
  // {"$t": "<tipo>", ...}. decode() reconstrói o valor; encode() faz o inverso
  // e é usado para conferir o que foi gravado.
  function decode(v) {
    if (v === null || typeof v !== 'object') return v;
    if (Array.isArray(v)) return v.map(decode);
    if (Object.prototype.hasOwnProperty.call(v, '$t')) {
      switch (v.$t) {
        case 'undefined': return undefined;
        case 'date': return new Date(v.v);
        case 'number':
          return v.v === 'NaN' ? NaN : (v.v === 'Infinity' ? Infinity : -Infinity);
        case 'arraybuffer': return base64ToBytes(v.v).buffer;
        case 'typedarray': {
          const bytes = base64ToBytes(v.v);
          if (v.k === 'DataView') return new DataView(bytes.buffer);
          const Ctor = TYPED[v.k];
          if (!Ctor) throw new Error('tipo de array desconhecido no arquivo: ' + v.k);
          return new Ctor(bytes.buffer, 0, bytes.byteLength / Ctor.BYTES_PER_ELEMENT);
        }
        case 'object': {
          const inner = {};
          for (const key of Object.keys(v.v)) setField(inner, key, decode(v.v[key]));
          return inner;
        }
        default: throw new Error('marca desconhecida no arquivo: ' + v.$t);
      }
    }
    const out = {};
    for (const key of Object.keys(v)) setField(out, key, decode(v[key]));
    return out;
  }

  function encode(v) {
    if (v === undefined) return { $t: 'undefined' };
    if (v === null || typeof v === 'string' || typeof v === 'boolean') return v;
    if (typeof v === 'number') return Number.isFinite(v) ? v : { $t: 'number', v: String(v) };
    if (v instanceof Date) return { $t: 'date', v: v.getTime() };
    if (v instanceof ArrayBuffer) {
      return { $t: 'arraybuffer', v: bytesToBase64(new Uint8Array(v)) };
    }
    if (ArrayBuffer.isView(v)) {
      const bytes = new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
      return { $t: 'typedarray', k: v.constructor.name, v: bytesToBase64(bytes) };
    }
    if (Array.isArray(v)) return Array.from(v, encode);
    if (typeof v === 'object') {
      const out = {};
      for (const key of Object.keys(v)) setField(out, key, encode(v[key]));
      return Object.prototype.hasOwnProperty.call(v, '$t') ? { $t: 'object', v: out } : out;
    }
    throw new Error('tipo não suportado: ' + typeof v);
  }

  // Igualdade profunda entre duas formas codificadas (a ordem das chaves não importa).
  function same(a, b) {
    if (a === b) return true;
    if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object') return false;
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    if (Array.isArray(a)) {
      if (a.length !== b.length) return false;
      for (let i = 0; i < a.length; i += 1) if (!same(a[i], b[i])) return false;
      return true;
    }
    const keysA = Object.keys(a);
    if (keysA.length !== Object.keys(b).length) return false;
    for (const key of keysA) {
      if (!Object.prototype.hasOwnProperty.call(b, key) || !same(a[key], b[key])) return false;
    }
    return true;
  }

  // -------------------------------------------------------------------- banco
  async function openExisting() {
    // indexedDB.open() CRIA o banco se ele não existir. Para nunca criar nada:
    // primeiro perguntamos se existe; e, se mesmo assim o navegador tentar
    // criar (onupgradeneeded), abortamos na hora.
    if (indexedDB.databases) {
      const list = await indexedDB.databases();
      if (!list.some((d) => d.name === DB_NAME)) {
        throw new Error('O banco "' + DB_NAME + '" não existe nesta página. Nada foi criado.');
      }
    }
    return new Promise((resolve, reject) => {
      const r = indexedDB.open(DB_NAME);
      let refused = false;
      r.onupgradeneeded = (event) => {
        refused = true;
        event.target.transaction.abort();
      };
      r.onsuccess = () => {
        if (refused) { r.result.close(); return; }
        resolve(r.result);
      };
      r.onerror = () => reject(refused
        ? new Error('O banco "' + DB_NAME + '" não existia nesta página. Nada foi criado.')
        : r.error);
      r.onblocked = () => reject(new Error('O banco está bloqueado por outra aba ou janela.'));
    });
  }

  function schemaProblems(db, expected) {
    const problems = [];
    if (expected.version !== null && expected.version !== undefined && db.version !== expected.version) {
      problems.push('versão do banco: aqui é ' + db.version + ', o arquivo espera ' + expected.version);
    }
    const names = Object.keys(expected.stores || {});
    for (const name of [CHATS, BLOBS]) {
      if (!names.includes(name)) problems.push('o arquivo não descreve o store "' + name + '"');
    }
    const present = names.filter((n) => db.objectStoreNames.contains(n));
    for (const name of names) {
      if (!db.objectStoreNames.contains(name)) problems.push('falta o store "' + name + '" aqui');
    }
    if (present.length === 0) return problems;
    const tx = db.transaction(present, 'readonly');
    for (const name of present) {
      const store = tx.objectStore(name);
      const want = expected.stores[name];
      if (JSON.stringify(store.keyPath) !== JSON.stringify(want.key_path)) {
        problems.push('store "' + name + '": chave ' + JSON.stringify(store.keyPath)
          + ', o arquivo espera ' + JSON.stringify(want.key_path));
      }
      if (store.autoIncrement !== Boolean(want.auto_increment)) {
        problems.push('store "' + name + '": autoIncrement diferente');
      }
      const have = Array.from(store.indexNames).sort();
      const wanted = Object.keys(want.indexes || {}).sort();
      if (JSON.stringify(have) !== JSON.stringify(wanted)) {
        problems.push('store "' + name + '": índices ' + JSON.stringify(have)
          + ', o arquivo espera ' + JSON.stringify(wanted));
        continue;
      }
      for (const indexName of wanted) {
        const index = store.index(indexName);
        const spec = want.indexes[indexName];
        if (JSON.stringify(index.keyPath) !== JSON.stringify(spec.key_path)
            || index.unique !== Boolean(spec.unique)
            || index.multiEntry !== Boolean(spec.multi_entry)) {
          problems.push('store "' + name + '": índice "' + indexName + '" diferente');
        }
      }
    }
    return problems;
  }

  // Pares (userId, orgId) das conversas que já estão no banco. Lemos só as
  // chaves de um índice: nenhuma conversa é carregada.
  async function currentIdentities(db) {
    const store = db.transaction(CHATS, 'readonly').objectStore(CHATS);
    const seen = new Map();
    const indexName = '[userId+orgId+surface]';
    if (store.indexNames.contains(indexName)) {
      await new Promise((resolve, reject) => {
        const cursorRequest = store.index(indexName).openKeyCursor();
        cursorRequest.onerror = () => reject(cursorRequest.error);
        cursorRequest.onsuccess = () => {
          const cursor = cursorRequest.result;
          if (!cursor) { resolve(); return; }
          seen.set(JSON.stringify([cursor.key[0], cursor.key[1]]), [cursor.key[0], cursor.key[1]]);
          cursor.continue();
        };
      });
    }
    return Array.from(seen.values());
  }

  // ---------------------------------------------------------------- simulação
  async function inspect(db, payload) {
    const plan = {
      problems: [], warnings: [], insert: [], skip: [], blobCount: 0, remap: null,
      identitiesHere: [], schemaChecked: false,
    };
    if (!payload || payload.format !== SUPPORTED_FORMAT) {
      plan.problems.push('arquivo em formato desconhecido (esperado: formato ' + SUPPORTED_FORMAT + ')');
      return plan;
    }
    if (payload.database !== DB_NAME) {
      plan.problems.push('o arquivo é de outro banco: ' + payload.database);
      return plan;
    }
    plan.problems.push(...schemaProblems(db, payload.schema || {}));
    plan.schemaChecked = true;
    if (plan.problems.length) return plan;

    const tx = db.transaction([CHATS, BLOBS], 'readonly');
    const existingChats = new Set(await request(tx.objectStore(CHATS).getAllKeys()));
    const existingBlobs = new Set(
      (await request(tx.objectStore(BLOBS).getAllKeys())).map((k) => JSON.stringify(k)));
    plan.identitiesHere = await currentIdentities(db);

    for (const entry of payload.conversations || []) {
      if (existingChats.has(entry.id)) {
        plan.skip.push(entry);
        continue;
      }
      const blobs = (entry.blobs || []).filter((b) => !existingBlobs.has(JSON.stringify(b.key)));
      plan.insert.push({ entry, blobs });
      plan.blobCount += blobs.length;
    }

    // Identidade: só propomos ajuste se o banco tiver UMA identidade e ela for
    // diferente da de alguma conversa a inserir.
    const here = plan.identitiesHere;
    const differs = (e) => here.length === 1
      && (e.userId !== here[0][0] || e.orgId !== here[0][1]);
    if (here.length === 0 && plan.insert.length) {
      plan.warnings.push('O banco não tem nenhuma conversa, então a identidade atual é desconhecida. '
        + 'As conversas seriam gravadas com o userId/orgId originais.');
    } else if (here.length > 1) {
      plan.warnings.push('O banco tem conversas de mais de uma identidade; userId/orgId não seriam ajustados.');
    } else if (plan.insert.some((item) => differs(item.entry))) {
      plan.remap = { userId: here[0][0], orgId: here[0][1] };
      plan.warnings.push('Identidade diferente: userId/orgId das conversas inseridas seriam '
        + 'ajustados para os do banco atual (' + here[0][0] + ' / ' + here[0][1] + ').');
    }
    for (const item of plan.insert) item.remap = Boolean(plan.remap && differs(item.entry));
    return plan;
  }

  function describe(plan) {
    const lines = [];
    const line = (e) => '   - ' + String(e.id).slice(0, 8) + '  ' + (e.title || 'Sem título')
      + '  (' + (e.messages === null || e.messages === undefined ? '?' : e.messages) + ' mensagens)';
    if (plan.problems.length) {
      lines.push('NÃO É POSSÍVEL GRAVAR:');
      for (const p of plan.problems) lines.push('   - ' + p);
      lines.push('Nada foi gravado.');
      return lines.join('\n');
    }
    lines.push('Esquema do banco: confere com o do arquivo.');
    lines.push('SERIAM INSERIDAS (' + plan.insert.length + '), com ' + plan.blobCount + ' anexo(s):');
    for (const item of plan.insert) {
      lines.push(line(item.entry) + (item.remap ? '  [userId/orgId ajustados]' : ''));
    }
    lines.push('JÁ EXISTEM e não seriam tocadas (' + plan.skip.length + '):');
    for (const e of plan.skip) lines.push(line(e));
    for (const w of plan.warnings) lines.push('AVISO: ' + w);
    lines.push('Nenhuma conversa existente seria alterada ou apagada.');
    return lines.join('\n');
  }

  // ----------------------------------------------------------------- gravação
  async function apply(db, plan) {
    const tx = db.transaction([CHATS, BLOBS], 'readwrite');
    const done = finished(tx);
    const chats = tx.objectStore(CHATS);
    const blobs = tx.objectStore(BLOBS);
    const written = [];
    try {
      for (const item of plan.insert) {
        const value = decode(item.entry.value);
        if (item.remap) {
          value.userId = plan.remap.userId;
          value.orgId = plan.remap.orgId;
        }
        chats.add(value); // add(): falha se a chave já existir; nunca sobrescreve
        written.push({ store: CHATS, key: item.entry.id, encoded: encode(value) });
        for (const blob of item.blobs) {
          const blobValue = decode(blob.value);
          blobs.add(blobValue);
          written.push({ store: BLOBS, key: blob.key, encoded: encode(blobValue) });
        }
      }
    } catch (error) {
      try { tx.abort(); } catch (ignored) { /* já abortada */ }
      await done.catch(() => {});
      throw error;
    }
    await done; // se qualquer add() falhar, a transação inteira é desfeita
    return written;
  }

  async function verify(db, written) {
    const mismatches = [];
    const tx = db.transaction([CHATS, BLOBS], 'readonly');
    for (const item of written) {
      const stored = await request(tx.objectStore(item.store).get(item.key));
      if (stored === undefined || !same(encode(stored), item.encoded)) {
        mismatches.push(item.store + ' ' + JSON.stringify(item.key));
      }
    }
    return mismatches;
  }

  async function rollback(db, written) {
    // Remove SOMENTE o que este script inseriu (chaves que não existiam antes).
    const tx = db.transaction([CHATS, BLOBS], 'readwrite');
    const done = finished(tx);
    for (const item of written) tx.objectStore(item.store).delete(item.key);
    await done;
  }

  async function writeAndVerify(db, plan) {
    if (plan.problems.length) throw new Error('há impedimentos; nada foi gravado');
    let written;
    try {
      written = await apply(db, plan);
    } catch (error) {
      throw new Error('A gravação falhou e NADA foi inserido (a transação foi desfeita): '
        + (error && error.message ? error.message : error));
    }
    const mismatches = await verify(db, written);
    if (mismatches.length) {
      await rollback(db, written);
      throw new Error('A conferência falhou em ' + mismatches.length
        + ' registro(s); o que foi inserido foi REMOVIDO. O banco está como antes.');
    }
    return {
      conversations: plan.insert.length,
      records: written.length,
      skipped: plan.skip.length,
    };
  }

  // ------------------------------------------------------------------- painel
  // Tudo é criado com createElement/textContent/style.setProperty: sem HTML em
  // texto e sem atributos de estilo, para funcionar mesmo com políticas de
  // segurança de conteúdo (CSP) restritas.
  const state = { db: null, plan: null, panel: null };

  function element(tag, text, styles) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    for (const [name, value] of Object.entries(styles || {})) node.style.setProperty(name, value);
    return node;
  }

  function close() {
    if (state.db) { state.db.close(); state.db = null; }
    if (state.panel) { state.panel.remove(); state.panel = null; }
    state.plan = null;
  }

  function buildPanel() {
    const panel = element('div', '', {
      position: 'fixed', top: '12px', right: '12px', 'z-index': '2147483647',
      width: 'min(520px, calc(100vw - 24px))', 'max-height': 'calc(100vh - 24px)',
      overflow: 'auto', background: '#ffffff', color: '#1a1a1a',
      border: '2px solid #1a1a1a', 'border-radius': '8px', padding: '14px',
      font: '13px/1.45 Segoe UI, Arial, sans-serif', 'box-shadow': '0 6px 24px rgba(0,0,0,.35)',
      'box-sizing': 'border-box',
    });
    panel.appendChild(element('div', 'office-chat-keeper: restauração seletiva',
      { 'font-weight': '700', 'font-size': '15px', 'margin-bottom': '6px' }));
    panel.appendChild(element('div',
      'Ferramenta não oficial. Nada é gravado antes de você ler a simulação e digitar '
      + CONFIRM_WORD + '. Conversas existentes nunca são alteradas.',
      { 'margin-bottom': '10px' }));

    const picker = element('input', '', { display: 'block', 'margin-bottom': '10px', width: '100%' });
    picker.type = 'file';
    picker.accept = '.json,application/json';
    panel.appendChild(element('label', '1. Escolha o arquivo dados-restauracao.json:',
      { display: 'block', 'font-weight': '600' }));
    panel.appendChild(picker);

    const log = element('pre', 'Aguardando o arquivo.', {
      'white-space': 'pre-wrap', 'word-break': 'break-word', background: '#f3f3f3',
      color: '#1a1a1a', padding: '8px', margin: '0 0 10px 0', 'border-radius': '4px',
      'max-height': '260px', overflow: 'auto', font: '12px/1.4 Consolas, monospace',
    });
    panel.appendChild(log);

    panel.appendChild(element('label', '2. Para gravar, digite ' + CONFIRM_WORD + ':',
      { display: 'block', 'font-weight': '600' }));
    const confirmBox = element('input', '', { width: '160px', 'margin-right': '8px', padding: '4px' });
    confirmBox.type = 'text';
    confirmBox.disabled = true;
    const writeButton = element('button', 'Gravar', { padding: '5px 14px', 'margin-right': '8px' });
    writeButton.disabled = true;
    const closeButton = element('button', 'Fechar', { padding: '5px 14px' });
    panel.appendChild(confirmBox);
    panel.appendChild(writeButton);
    panel.appendChild(closeButton);

    const refresh = () => {
      const ready = state.plan && !state.plan.problems.length && state.plan.insert.length > 0;
      confirmBox.disabled = !ready;
      writeButton.disabled = !(ready && confirmBox.value.trim() === CONFIRM_WORD);
    };
    confirmBox.addEventListener('input', refresh);
    closeButton.addEventListener('click', close);

    picker.addEventListener('change', async () => {
      state.plan = null;
      refresh();
      const file = picker.files && picker.files[0];
      if (!file) return;
      try {
        log.textContent = 'Lendo ' + file.name + ' (' + Math.round(file.size / 1024) + ' KB)...';
        const payload = JSON.parse(await file.text()); // leitura local; nada é enviado
        if (!state.db) state.db = await openExisting();
        state.plan = await inspect(state.db, payload);
        log.textContent = 'SIMULAÇÃO (nada foi gravado)\n' + describe(state.plan);
      } catch (error) {
        log.textContent = 'ERRO: ' + (error && error.message ? error.message : error)
          + '\nNada foi gravado.';
      }
      refresh();
    });

    writeButton.addEventListener('click', async () => {
      writeButton.disabled = true;
      confirmBox.disabled = true;
      const plan = state.plan;
      state.plan = null; // cada gravação exige uma nova simulação
      try {
        log.textContent += '\n\nGravando...';
        const result = await writeAndVerify(state.db, plan);
        log.textContent += '\nCONCLUÍDO e conferido: ' + result.conversations + ' conversa(s), '
          + result.records + ' registro(s) inserido(s); ' + result.skipped + ' já existiam.'
          + '\nFeche e reabra o painel do suplemento para ver as conversas.'
          + '\nDepois rode: office-chat-keeper run';
      } catch (error) {
        log.textContent += '\nERRO: ' + (error && error.message ? error.message : error);
      }
      confirmBox.value = '';
    });

    document.body.appendChild(panel);
    return panel;
  }

  state.panel = buildPanel();

  // Para ensaios automatizados (sem o painel): as mesmas funções, expostas.
  window.__officeChatKeeperRestore = {
    close,
    inspect: async (payload) => {
      if (!state.db) state.db = await openExisting();
      state.plan = await inspect(state.db, payload);
      return {
        text: describe(state.plan),
        problems: state.plan.problems,
        warnings: state.plan.warnings,
        insert: state.plan.insert.map((item) => item.entry.id),
        skip: state.plan.skip.map((entry) => entry.id),
        remap: state.plan.remap,
      };
    },
    write: async (confirmation) => {
      if (confirmation !== CONFIRM_WORD) throw new Error('confirmação ausente; nada foi gravado');
      if (!state.plan) throw new Error('faça a simulação (inspect) antes; nada foi gravado');
      const plan = state.plan;
      state.plan = null;
      return writeAndVerify(state.db, plan);
    },
    _internals: { decode, encode, same },
  };
  console.info('office-chat-keeper: painel de restauração aberto. Nada foi gravado.');
})();
