import { createHash } from 'node:crypto';
import { DOMParser, XMLSerializer } from '@xmldom/xmldom';

export const hash = bytes => createHash('sha256').update(bytes).digest('hex');
export const json = value => JSON.stringify(value, null, 2) + '\n';
export const escape = value => String(value).replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char]);

export function identifier(value) {
  if (typeof value !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$/.test(value))
    throw new Error('Invalid artifact ID');
  return value;
}

export function sourceIdentifier(value) {
  if (typeof value !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/.test(value))
    throw new Error('Invalid source ID');
  return value;
}

export function label(value, maximum = 160) {
  if (typeof value !== 'string' || !value.trim() || value.length > maximum || /[\x00-\x1f\x7f]/u.test(value))
    throw new Error('Invalid label');
  return value;
}

export function number(value, minimum, maximum) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < minimum || value > maximum)
    throw new Error('Numeric value is outside the allowed range');
  return value;
}

export function object(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !keys.includes(key)))
    throw new Error('Invalid fields for the visual specification');
  return value;
}

// SVG is parsed as XML, and emitted again after removing or rejecting constructs
// beyond the static subset. Its document never executes in a page's origin.
export function safeSvg(source) {
  if (Buffer.byteLength(source) > 2_000_000 || /<!\s*(?:DOCTYPE|ENTITY)|<\?/i.test(source))
    throw new Error('SVG exceeds its resource or XML limits');
  const document = new DOMParser({ onError: (_, message) => { throw new Error(message); } })
    .parseFromString(source, 'image/svg+xml');
  const root = document.documentElement;
  if (root.localName !== 'svg' || root.namespaceURI !== 'http://www.w3.org/2000/svg')
    throw new Error('Expected an SVG root');

  const tags = new Set([
    'svg', 'g', 'defs', 'clipPath', 'marker', 'path', 'line', 'polyline',
    'polygon', 'circle', 'ellipse', 'rect', 'text', 'tspan', 'title', 'desc',
    'style', 'symbol', 'use', 'filter', 'feDropShadow', 'linearGradient', 'stop',
  ]);
  const localReference = /^#[a-zA-Z0-9][a-zA-Z0-9_.:-]*$/;
  const safeStyle = value => {
    const noLocalUrls = value.replace(/url\(\s*['"]?(#[\w.:-]+)['"]?\s*\)/gi, '');
    return !/url\s*\(|@import|@font-face|expression\s*\(|<|\\/i.test(noLocalUrls);
  };
  const elements = Array.from(root.getElementsByTagName('*'));
  if (elements.length > 12_000) throw new Error('SVG contains too many elements');
  for (const element of [root, ...elements]) {
    if (!tags.has(element.localName) || element.namespaceURI !== root.namespaceURI)
      throw new Error('Unsupported SVG element ' + element.localName);
    if (element.localName === 'style' && !safeStyle(element.textContent))
      throw new Error('Unsafe SVG stylesheet');
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.localName.toLowerCase();
      const value = attribute.value;
      if (name.startsWith('on') || ['base', 'src', 'srcset', 'cursor'].includes(name) ||
          (name === 'href' && (element.localName !== 'use' || !localReference.test(value))) ||
          (name === 'style' && !safeStyle(value)) ||
          (/url\s*\(/i.test(value) && !safeStyle(value)))
        throw new Error('Unsafe SVG attribute ' + name);
    }
  }
  // Vega sets CSS-pixel height and width. Generate a viewBox if one is absent.
  const dimensions = root.getAttribute('viewBox')?.trim().split(/[\s,]+/u).map(Number) ??
    [0, 0, Number(root.getAttribute('width')), Number(root.getAttribute('height'))];
  if (dimensions.length !== 4 || dimensions.some(value => !Number.isFinite(value)) ||
      dimensions[2] <= 0 || dimensions[2] > 4096 || dimensions[3] <= 0 || dimensions[3] > 4096 ||
      dimensions[2] * dimensions[3] > 12_000_000)
    throw new Error('SVG viewBox is missing or outside its size limit');
  root.setAttribute('width', String(Math.ceil(dimensions[2])));
  root.setAttribute('height', String(Math.ceil(dimensions[3])));
  root.setAttribute('viewBox', dimensions.join(' '));
  const svg = new XMLSerializer().serializeToString(root);
  return { svg, width: Math.ceil(dimensions[2]), height: Math.ceil(dimensions[3]) };
}
