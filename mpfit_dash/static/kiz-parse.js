/* Разбор кода маркировки «Честный знак» (GS1 DataMatrix) и штрихкода.
   Работает и когда сканер потерял символ-разделитель GS (0x1D).
   Подключается в браузере (window.KIZ) и в node для тестов (module.exports). */
(function (root) {
  const GS = '\x1d';
  // AI фиксированной длины (без разделителя после себя)
  const FIXED = { '01': 14, '17': 6, '11': 6, '7003': 10, '3103': 6, '3100': 6 };
  // AI, после которых в кодах ЧЗ всегда идёт GS (переменная длина)
  const VAR = ['21', '91', '92', '93', '240', '10'];

  // Раскладка ЙЦУКЕН → QWERTY: если сканер «печатает» в русской раскладке
  const RU = 'йцукенгшщзхъфывапролджэячсмитьбюёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮЁ.,"№;:?';
  const EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>~/?@#$^&";
  function delayout(s) {
    if (!/[а-яё]/i.test(s)) return s;
    let o = '';
    for (const ch of s) { const i = RU.indexOf(ch); o += i >= 0 ? EN[i] : ch; }
    return o;
  }

  function clean(raw) {
    let s = String(raw || '').replace(/[\r\n]+$/g, '');
    s = delayout(s);
    s = s.replace(/^\]d2|^\]C1|^\]Q3/, '');           // префикс символики
    s = s.replace(/<GS>|\{GS\}|\\x1d|\\u001d|~1|␝/gi, GS);
    s = s.replace(/^\x1d/, '');
    return s;
  }

  function parseYYMMDD(v) {
    if (!/^\d{6}$/.test(v)) return null;
    let y = 2000 + +v.slice(0, 2), m = +v.slice(2, 4), d = +v.slice(4, 6);
    if (m < 1 || m > 12) return null;
    if (d === 0) d = new Date(Date.UTC(y, m, 0)).getUTCDate();   // 00 = конец месяца
    return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
  }

  // Разбор хвоста после 01+GTIN: список [ai, value]
  function splitWithGS(rest) {
    const out = [];
    for (let part of rest.split(GS)) {
      while (part.length) {
        const ai4 = part.slice(0, 4), ai2 = part.slice(0, 2), ai3 = part.slice(0, 3);
        if (FIXED[ai4]) { out.push([ai4, part.slice(4, 4 + FIXED[ai4])]); part = part.slice(4 + FIXED[ai4]); continue; }
        if (FIXED[ai2]) { out.push([ai2, part.slice(2, 2 + FIXED[ai2])]); part = part.slice(2 + FIXED[ai2]); continue; }
        if (VAR.includes(ai3)) { out.push([ai3, part.slice(3)]); break; }
        out.push([ai2, part.slice(2)]); break;
      }
    }
    return out;
  }

  // Без GS: подбираем длину серийного номера так, чтобы дальше шла
  // знакомая структура ЧЗ (91+4 и 92…, или 93+4, с необязательными 17/7003).
  function splitNoGS(rest) {
    if (!rest.startsWith('21')) return null;
    const body = rest.slice(2);
    const lens = [13, 6, 7, 8, 11, 12, 14, 15, 16, 17, 18, 19, 20, 5, 9, 10];
    for (const L of lens) {
      const serial = body.slice(0, L);
      let tail = body.slice(L);
      const out = [['21', serial]];
      let m;
      if ((m = tail.match(/^17(\d{6})/))) { out.push(['17', m[1]]); tail = tail.slice(8); }
      else if ((m = tail.match(/^7003(\d{10})/))) { out.push(['7003', m[1]]); tail = tail.slice(14); }
      if ((m = tail.match(/^91(.{4})92(.{44}|.{88})$/))) { out.push(['91', m[1]], ['92', m[2]]); return out; }
      if ((m = tail.match(/^93(.{4})$/))) { out.push(['93', m[1]]); return out; }
      if ((m = tail.match(/^93(.{4})3103(\d{6})$/))) { out.push(['93', m[1]], ['3103', m[2]]); return out; }
      if (tail === '' && L === body.length && L >= 6) return out;    // только 01+21
    }
    return null;
  }

  function parseKiz(raw) {
    const s = clean(raw);
    if (!/^01\d{14}/.test(s) || s.length < 24) return null;
    const gtin = s.slice(2, 16);
    const rest = s.slice(16);
    const hadGS = rest.includes(GS);
    const parts = hadGS ? splitWithGS(rest) : splitNoGS(rest);
    if (!parts || !parts.length || parts[0][0] !== '21') return { ok: false, gtin, raw: s, error: 'не удалось разобрать код' };
    const f = Object.fromEntries(parts);
    let expiry = null, expirySrc = null;
    if (f['17']) { expiry = parseYYMMDD(f['17']); expirySrc = 'КИЗ (AI 17)'; }
    else if (f['7003']) { expiry = parseYYMMDD(f['7003'].slice(0, 6)); expirySrc = 'КИЗ (AI 7003)'; }
    // строка для DataMatrix: FNC1 в начале и после каждого поля переменной длины
    let dm = '^FNC1' + '01' + gtin, prevVar = false;
    for (const [ai, v] of parts) {
      if (prevVar) dm += '^FNC1';
      dm += ai + v.replace(/\^/g, '^094');
      prevVar = !FIXED[ai];
    }
    // каноническая запись с GS — для журнала и сверки дублей
    let canon = '01' + gtin; prevVar = false;
    for (const [ai, v] of parts) { if (prevVar) canon += GS; canon += ai + v; prevVar = !FIXED[ai]; }
    return { ok: true, gtin, serial: f['21'], parts, expiry, expirySrc, dm, canon,
             human: `(01)${gtin}(21)${f['21']}`, hadGS };
  }

  function eanCheck(d12) {
    let s = 0;
    for (let i = 0; i < 12; i++) s += +d12[i] * (i % 2 ? 3 : 1);
    return (10 - (s % 10)) % 10;
  }

  // Что отсканировали: КИЗ, штрихкод или ничего понятного
  function classify(raw) {
    const s = clean(raw).trim();
    if (!s) return { type: 'empty' };
    if (/^01\d{14}21/.test(s) && s.length >= 24) return { type: 'kiz', kiz: parseKiz(s) };
    const digits = s.replace(/\s/g, '');
    if (/^\d{8}$|^\d{12,14}$/.test(digits)) {
      let ean = digits.length === 14 && digits[0] === '0' ? digits.slice(1) : digits;
      const valid = ean.length === 13 && eanCheck(ean) === +ean[12];
      return { type: 'barcode', code: ean, bcid: valid ? 'ean13' : 'code128' };
    }
    if (/^[\x20-\x7e]{4,40}$/.test(s)) return { type: 'barcode', code: s, bcid: 'code128' };
    return { type: 'unknown', raw: s };
  }

  const api = { parseKiz, classify, clean, eanCheck, parseYYMMDD, GS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.KIZ = api;
})(typeof window !== 'undefined' ? window : globalThis);
