// just enough header parsing to label quarantined messages in the dashboard
// (From / Subject / Date), including RFC 2047 encoded-words

export interface HeaderSummary {
  from: string;
  subject: string;
  date: string;
}

const ENCODED_WORD = /=\?([^?\s]+)\?([bBqQ])\?([^?\s]*)\?=/g;

function decodeWord(charset: string, enc: string, text: string): string {
  let bytes: Uint8Array;
  if (enc.toUpperCase() === "B") {
    const bin = atob(text);
    bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  } else {
    const s = text.replace(/_/g, " ");
    const out: number[] = [];
    for (let i = 0; i < s.length; i++) {
      if (s[i] === "=" && /^[0-9a-fA-F]{2}$/.test(s.slice(i + 1, i + 3))) {
        out.push(parseInt(s.slice(i + 1, i + 3), 16));
        i += 2;
      } else {
        out.push(s.charCodeAt(i) & 0xff);
      }
    }
    bytes = Uint8Array.from(out);
  }
  // RFC 2231 language suffix: charset*lang
  return new TextDecoder(charset.split("*")[0]).decode(bytes);
}

export function decodeEncodedWords(s: string): string {
  // whitespace between adjacent encoded-words is not part of the text
  const joined = s.replace(/(\?=)\s+(?==\?)/g, "$1");
  return joined.replace(ENCODED_WORD, (whole, charset, enc, text) => {
    try {
      return decodeWord(charset, enc, text);
    } catch {
      return whole;
    }
  });
}

export function summarizeHeaders(bytes: Uint8Array): HeaderSummary {
  const text = new TextDecoder("utf-8").decode(bytes);
  const end = text.search(/\r?\n\r?\n/);
  const head = (end >= 0 ? text.slice(0, end) : text).replace(/\r?\n[ \t]+/g, " ");
  const get = (name: string): string => {
    const m = new RegExp(`^${name}:[ \\t]*(.*)$`, "im").exec(head);
    return m ? decodeEncodedWords(m[1].trim()).slice(0, 300) : "";
  };
  return { from: get("From"), subject: get("Subject"), date: get("Date") };
}
