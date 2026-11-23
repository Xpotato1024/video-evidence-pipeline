import katex from 'katex';
import { escape, identifier, label, sourceIdentifier } from './common.mjs';

const messages = {
  ja: {
    assumptions: '成立条件', symbols: '記号', sourceList: '出典',
    current_lecture: '今回の授業', previous_lecture: '過去の授業', supplement: '補足',
    page: 'ページ', region: '領域', math: '数式', values: 'グラフの数値',
    symbolic: '文字式', substitution: '数値代入', intermediate: '途中計算', result: '結果',
    figureTypes: {
      source_crop: '原資料の一部', derived_diagram: '模式図', theoretical: '理論値',
      measured: '観測値', illustrative: '説明用の例',
    },
  },
  en: {
    assumptions: 'Assumptions', symbols: 'Symbols', sourceList: 'Sources',
    current_lecture: 'Current lecture', previous_lecture: 'Previous lecture', supplement: 'Supplement',
    page: 'Page', region: 'Region', math: 'Equation', values: 'Chart data',
    symbolic: 'Symbolic equation', substitution: 'Substitution', intermediate: 'Intermediate calculation', result: 'Result',
    figureTypes: {
      source_crop: 'Source crop', derived_diagram: 'Schematic', theoretical: 'Theoretical values',
      measured: 'Observed values', illustrative: 'Illustration',
    },
  },
};

const blockedMath = /\\(?:href|url|includegraphics|html\w*|class|style|color|textcolor|definecolor|require|def|gdef|edef|xdef|let|futurelet|newcommand|renewcommand|providecommand|global)\b/u;
const unrenderedMath = /\$\$|\\(?:\(|\[|frac\b|dfrac\b|partial\b|begin\{|sqrt\b|vec\b|sum\b)/u;

export function mathml(source) {
  if (typeof source !== 'string' || !source.trim() || source.length > 4096 ||
      /[`\x00-\x09\x0b-\x1f\x7f]/u.test(source) || blockedMath.test(source))
    throw new Error('Invalid or unsupported TeX source');
  const result = katex.renderToString(source, {
    output: 'mathml', displayMode: true, trust: false, strict: 'error',
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

function prose(text) {
  if (typeof text !== 'string' || unrenderedMath.test(text)) throw new Error('Use a numbered equation or step for TeX');
  return escape(text);
}

const markdown = (text, table = false) => prose(text)
  .replace(/[\\`*_{}\[\]$]/gu, '\\$&')
  .replace(/(^|\n)(?=[#>+\-]|\d+[.)])/gu, table ? '$1' : '$1\\')
  .replace(/(^|\n)(import|export)\b/gu, (_, line, keyword) =>
    line + (keyword === 'import' ? '&#105;mport' : '&#101;xport'))
  .replace(/\|/gu, table ? '\\|' : '|')
  .replace(/\r?\n/gu, table ? ' / ' : '\n');

const htmlTable = (headers, rows, caption) =>
  `<div class="table-scroll" tabindex="0" aria-label="${escape(caption)}"><table><caption>${escape(caption)}</caption><thead><tr>` +
  headers.map(value => `<th scope="col">${escape(value)}</th>`).join('') + '</tr></thead><tbody>' +
  rows.map(row => '<tr>' + row.map(value => `<td>${escape(value)}</td>`).join('') + '</tr>').join('') +
  '</tbody></table></div>';

function mdTable(headers, rows) {
  const rowText = row => `| ${row.map(value => markdown(String(value), true)).join(' | ')} |`;
  return [
    rowText(headers),
    `| ${headers.map(() => '---').join(' | ')} |`,
    ...rows.map(rowText),
  ].join('\n');
}

function sourceDetails(source, words) {
  const locator = source.locator ?? {};
  const fragments = [];
  if (locator.session_id) fragments.push(locator.session_id);
  if (locator.ref_id) fragments.push(locator.ref_id);
  if (locator.page != null) fragments.push(words.page + ' ' + locator.page);
  if (locator.start_ms != null) {
    const time = milliseconds => {
      const hours = String(Math.floor(milliseconds / 3_600_000)).padStart(2, '0');
      return hours + ':' + new Date(milliseconds % 3_600_000).toISOString().slice(14, 23);
    };
    fragments.push(time(locator.start_ms) + (locator.end_ms != null ? ' – ' + time(locator.end_ms) : ''));
  }
  if (Array.isArray(locator.region)) fragments.push(`${words.region} (${locator.region.join(', ')})`);
  if (locator.note) fragments.push(locator.note);
  return fragments.join(' · ');
}

export function createDocument(content, equations, figures, stepEquations) {
  const lang = label(content.language ?? 'en', 30);
  if (!/^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/u.test(lang)) throw new Error('Invalid language tag');
  const words = lang.startsWith('ja') ? messages.ja : messages.en;
  const sources = new Map(content.sources.map(source => [sourceIdentifier(source.id), source]));
  const referenceIds = new Set([
    ...content.blocks, ...content.equations, ...content.figures,
    ...content.blocks.filter(block => block.type === 'steps').flatMap(block => block.steps),
  ].flatMap(item => item.source_refs ?? []));
  const usedSources = [...sources.values()].filter(source => referenceIds.has(source.id));
  const references = work => (work.source_refs ?? []).map(id => {
    const source = sources.get(id);
    if (!source || !words[source.kind]) throw new Error('Unknown source reference ' + id);
    return source;
  });
  const htmlReferences = (work, omit = []) => {
    const known = references(work).filter(source => !omit.includes(source.id));
    if (!known.length) return '';
    return `<p class="source-link">${words.sourceList} ${known.map(source =>
      `<a href="#source-${source.id}">${escape(words[source.kind])} · ${escape(source.title)}${source.locator?.page != null ? ' · ' + words.page + ' ' + escape(source.locator.page) : ''}</a>`).join(' / ')}</p>`;
  };
  const mdReferences = (work, omit = []) => {
    const known = references(work).filter(source => !omit.includes(source.id));
    return known.length ? '\n\n' + words.sourceList + ' ' +
      known.map(source => markdown(words[source.kind] + ' · ' + source.title + ' [' + source.id + ']')).join(' / ') : '';
  };
  const eqHtml = (expression, id, kind = 'math') =>
    `<div class="equation" tabindex="0" role="group" aria-label="${words[kind]} ${escape(id)}">${expression}</div>`;
  const mdMath = (tex, id) => '```math id="' + id + '"\n' + tex + '\n```';
  const eqMeta = equation => {
    const metadata = [];
    if (equation.assumptions?.length)
      metadata.push([`${words.assumptions}`, equation.assumptions.join(' / ')]);
    if (Object.keys(equation.symbol_table ?? {}).length)
      metadata.push([`${words.symbols}`, Object.entries(equation.symbol_table).map(([symbol, definition]) =>
        symbol + ' = ' + definition).join(' / ')]);
    return metadata;
  };

  function stepHtml(block, step) {
    const parts = stepEquations.get(block.id).get(step.id);
    return `<li id="${block.id}-${step.id}"><p class="step-title">${prose(step.title)}</p><p>${prose(step.rationale)}</p>` +
      [...parts.entries()].map(([kind, expression]) =>
        `<div class="step-equation"><p>${words[kind]}</p>${eqHtml(expression, step.id, kind)}</div>`).join('') +
      htmlReferences(step) + '</li>';
  }

  function html(block, mode) {
    const id = identifier(block.id);
    const refs = htmlReferences(block);
    switch (block.type) {
      case 'heading': {
        const level = Math.min(6, Math.max(2, block.level));
        if (!Number.isInteger(level)) throw new Error('Invalid heading level');
        return `<h${level} id="block-${id}">${prose(block.text)}</h${level}>${refs}`;
      }
      case 'paragraph': return `<p id="block-${id}" class="prose">${prose(block.text)}</p>${refs}`;
      case 'callout': return `<aside id="block-${id}"><p><strong>${prose(block.title)}</strong></p><p class="prose">${prose(block.text)}</p>${refs}</aside>`;
      case 'table': return `<section id="block-${id}">${htmlTable(block.headers, block.rows, block.caption)}${refs}</section>`;
      case 'equation': {
        const equation = equations.get(block.equation_id);
        if (!equation) throw new Error('Unknown equation ' + block.equation_id);
        return `<div id="block-${id}" class="equation-block">${eqHtml(equation.expression, equation.id)}` +
          eqMeta(equation).map(([name, value]) => `<p class="equation-meta"><strong>${name}</strong> ${escape(value)}</p>`).join('') +
          htmlReferences(equation) + `</div>${htmlReferences(block, equation.source_refs)}`;
      }
      case 'figure': {
        const figure = figures.get(block.figure_id);
        if (!figure) throw new Error('Unknown figure ' + block.figure_id);
        const src = mode === 'standalone' ? figure.dataUri : figure.path;
        const data = figure.data ? `<details><summary>${words.values}</summary>` +
          htmlTable(figure.data.headers, figure.data.rows, words.values) + '</details>' : '';
        const kind = words.figureTypes[figure.factuality] ?? '';
        return `<figure id="block-${id}"><div class="figure-scroll" tabindex="0" aria-label="${escape(figure.alt)}">` +
          `<img src="${src}" alt="${escape(figure.alt)}" width="${figure.width}" height="${figure.height}">\n</div>` +
          `<figcaption>${escape(figure.caption)}${kind ? '（' + kind + '）' : ''}</figcaption>${htmlReferences(figure)}${data}</figure>${htmlReferences(block, figure.source_refs)}`;
      }
      case 'steps': return `<ol class="steps" id="block-${id}">${block.steps.map(step => stepHtml(block, step)).join('')}</ol>${refs}`;
      default: throw new Error('Unknown lecture block ' + block.type);
    }
  }

  function md(block, mode) {
    const refs = mdReferences(block);
    switch (block.type) {
      case 'heading':
        return '#'.repeat(Math.max(2, block.level)) + ' ' + markdown(block.text) + refs;
      case 'paragraph': return markdown(block.text) + refs;
      case 'callout': return mode === 'mdx'
        ? `<Callout title="${escape(block.title)}">\n\n${markdown(block.text)}${refs}\n\n</Callout>`
        : '> **' + markdown(block.title) + '**\n>\n' + markdown(block.text).replaceAll('\n', '\n> ') + refs;
      case 'table':
        return '**' + markdown(block.caption) + '**\n\n' + mdTable(block.headers, block.rows) + refs;
      case 'equation': {
        const equation = equations.get(block.equation_id);
        return mdMath(equation.tex, equation.id) + eqMeta(equation).map(([name, value]) =>
          `\n\n**${name}** ${markdown(value)}`).join('') + mdReferences(equation) + mdReferences(block, equation.source_refs);
      }
      case 'figure': {
        const figure = figures.get(block.figure_id);
        const view = mode === 'mdx'
          ? `<Figure assetId="${figure.id}" src="${figure.path}" alt="${escape(figure.alt)}" caption="${escape(figure.caption)}" />`
          : `![${markdown(figure.alt)}](${figure.path})\n\n${markdown(figure.caption)}`;
        return view + mdReferences(figure) + mdReferences(block, figure.source_refs) +
          (figure.data ? `\n\n[${words.values}](data/${figure.id}.csv)` : '');
      }
      case 'steps': {
        const steps = block.steps.map((step, index) => {
          const parts = ['symbolic', 'substitution', 'intermediate', 'result'].filter(kind => step[kind] != null);
          const body = [markdown(step.rationale), ...parts.map(kind =>
            '**' + words[kind] + '**\n\n' + mdMath(step[kind], `${block.id}-${step.id}-${kind}`))].join('\n\n') + mdReferences(step);
          return mode === 'mdx' ? `<Step title="${escape(step.title)}">\n\n${body}\n\n</Step>`
            : `${index + 1}. **${markdown(step.title)}**\n\n` + body.split('\n').map(line => line ? '   ' + line : '').join('\n');
        }).join('\n\n');
        return (mode === 'mdx' ? '<Steps>\n\n' + steps + '\n\n</Steps>' : steps) + refs;
      }
      default: throw new Error('Unknown lecture block ' + block.type);
    }
  }

  function renderHtml(mode, css) {
    const list = usedSources.map(source => {
      const details = sourceDetails(source, words);
      return `<li id="source-${source.id}"><strong>${escape(words[source.kind])} · ${escape(source.title)}</strong>` +
        (details ? `<p>${escape(details)}</p>` : '') + '</li>';
    }).join('');
    const style = mode === 'standalone' ? `<style>${css}</style>` : '<link rel="stylesheet" href="lesson.css">';
    const images = mode === 'standalone' ? 'data:' : "'self'";
    const styles = mode === 'standalone' ? "'unsafe-inline'" : "'self'";
    const policy = `default-src 'none'; style-src ${styles}; img-src ${images}; base-uri 'none'; form-action 'none'`;
    return `<!doctype html>\n<html lang="${lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><meta http-equiv="Content-Security-Policy" content="${policy}"><title>${prose(content.title)}</title>${style}</head>\n<body><main><h1>${prose(content.title)}</h1>\n` +
      content.blocks.map(block => html(block, mode)).join('\n') +
      `\n<section class="sources"><h2>${words.sourceList}</h2><ul>${list}</ul></section></main></body></html>\n`;
  }

  function renderMarkdown(mode) {
    const list = usedSources.map(source => {
      const details = sourceDetails(source, words);
      return `- ${markdown(source.id + ' · ' + words[source.kind] + ' · ' + source.title + (details ? ' · ' + details : ''))}`;
    }).join('\n');
    const blocks = content.blocks.map(block =>
      (mode === 'mdx' ? `{/* vep:block ${identifier(block.id)} */}\n\n` : '') + md(block, mode));
    return `# ${markdown(content.title)}\n\n` + blocks.join('\n\n') +
      (mode === 'mdx' ? '\n\n{/* vep:sources */}' : '') + `\n\n## ${words.sourceList}\n\n${list}\n`;
  }
  return { renderHtml, renderMarkdown };
}
