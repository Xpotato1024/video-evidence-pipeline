import katex from 'katex';
import { escape } from '../common.mjs';

const blockedMath = /\\(?:href|url|includegraphics|html\w*|class|style|color|textcolor|definecolor|require|def|gdef|edef|xdef|let|futurelet|newcommand|renewcommand|providecommand|global)\b/u;
const unrenderedMath = /\$\$|\\(?:\(|\[|frac\b|dfrac\b|partial\b|begin\{|sqrt\b|vec\b|sum\b)/u;

export function mathml(source, displayMode = true) {
  if (typeof source !== 'string' || !source.trim() || source.length > 4096 ||
      /[`\x00-\x09\x0b-\x1f\x7f]/u.test(source) || blockedMath.test(source))
    throw new Error('Invalid or unsupported TeX source');
  const result = katex.renderToString(source, {
    output: 'mathml', displayMode, trust: false, strict: 'error',
    throwOnError: true, maxExpand: 100, maxSize: 8,
  });
  // The editable bundle holds .tex files and the reading view holds
  // presentation MathML plus accessibility labels supplied by the document.
  const math = /<math(?:\s[^>]*)?>[\s\S]*<\/math>/u.exec(result)?.[0];
  if (!math || /<merror|<script|<(?:maction|mglyph)\b|(?:href|src)=/iu.test(math) ||
      !/<m(?:i|n|o|frac|table|space)\b/u.test(math))
    throw new Error('KaTeX returned invalid presentation MathML');
  return math.replace(/<annotation\b[^>]*>[\s\S]*?<\/annotation>/gu, '');
}

export function prose(text) {
  if (typeof text !== 'string' || unrenderedMath.test(text)) throw new Error('Use a numbered equation or step for TeX');
  return escape(text);
}

// Portable prose has a deliberately small inline vocabulary. Every text leaf is
// escaped and links can only target blocks in the same verified document.
export function inline(text, targets = new Set(), equationLink) {
  const token = /\\\(([^\n]*?)\\\)|\*\*([^*\n]+)\*\*|\[([^\[\]\n]+)\]\(#block-([A-Za-z0-9][A-Za-z0-9._-]{0,79})\)|@eq\(([A-Za-z0-9][A-Za-z0-9._-]{0,79})\)/gu;
  let cursor = 0;
  let out = '';
  for (const match of text.matchAll(token)) {
    out += prose(text.slice(cursor, match.index));
    const [, tex, strong, link, id, equationId] = match;
    if (tex !== undefined) out += `<span class="inline-math">${mathml(tex, false)}</span>`;
    else if (strong !== undefined) out += '<strong>' + inline(strong, targets, equationLink) + '</strong>';
    else if (equationId !== undefined) out += equationLink ? equationLink(equationId) : prose(match[0]);
    else {
      if (!targets.has(id)) throw new Error('Missing lecture link target: ' + id);
      out += `<a href="#block-${id}">${prose(link)}</a>`;
    }
    cursor = match.index + match[0].length;
  }
  return out + prose(text.slice(cursor));
}

// Older editable packages store bare symbol names. Preserve any vector styling
// actually used by the equation; descriptive glossary keys stay plain text.
export function symbolMarkup(key, tex) {
  if (!/^[A-Za-z\p{Script=Greek}](?:_[A-Za-z0-9\p{Script=Greek}]+)?$/u.test(key)) return key;
  const greek = { 'θ': '\\theta', 'φ': '\\phi', 'ψ': '\\psi', 'Ω': '\\Omega',
    'μ': '\\mu', 'ν': '\\nu', 'ρ': '\\rho' };
  const [letter, index] = key.split('_');
  const base = greek[letter] ?? letter;
  const sub = index ? '_{' + (greek[index] ?? index) + '}' : '';
  const decorated = [...tex.matchAll(/(\\(?:mathbf|boldsymbol|vec))\s*(?:\{([a-zA-Z])\}|([a-zA-Z]))(?:_(?:\{([^{}\s]+)\}|(\\[A-Za-z]+|[A-Za-z0-9])))?/gu)]
    .find(m => (m[2] ?? m[3]) === base && (m[4] ?? m[5]) === (greek[index] ?? index));
  const expression = decorated ? decorated[1] + '{' + base + '}' + sub : base + sub;
  return '\\(' + expression + '\\)';
}

export const markdown = (text, table = false) => escape(text)
  .replace(/[\\`*_{}\[\]$]/gu, '\\$&')
  .replace(/(^|\n)(?=[#>+\-]|\d+[.)])/gu, table ? '$1' : '$1\\')
  .replace(/(^|\n)(import|export)\b/gu, (_, line, keyword) =>
    line + (keyword === 'import' ? '&#105;mport' : '&#101;xport'))
  .replace(/\|/gu, table ? '\\|' : '|')
  .replace(/\r?\n/gu, table ? ' / ' : '\n');
