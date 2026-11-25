import { inline, markdown, prose, symbolMarkup } from './document/inline.mjs';
export { inline, mathml } from './document/inline.mjs';
import { escape, identifier, label, sourceIdentifier } from './common.mjs';

const messages = {
  ja: {
    assumptions: '成立条件', symbols: '記号', sourceList: '出典',
    current_lecture: '今回の授業', previous_lecture: '過去の授業', previous_assignment: '過去の課題', clarification: '追加確認', supplement: '補足',
    page: 'ページ', region: '領域', math: '数式', values: 'グラフの数値',
    symbolic: '文字式', substitution: '数値代入', intermediate: '途中計算', result: '結果',
    figureTypes: {
      source_crop: '原資料の一部', derived_diagram: '模式図', theoretical: '理論値',
      measured: '観測値', illustrative: '説明用の例',
    },
  },
  en: {
    assumptions: 'Assumptions', symbols: 'Symbols', sourceList: 'Sources',
    current_lecture: 'Current lecture', previous_lecture: 'Previous lecture', previous_assignment: 'Previous assignment', clarification: 'Clarification', supplement: 'Supplement',
    page: 'Page', region: 'Region', math: 'Equation', values: 'Chart data',
    symbolic: 'Symbolic equation', substitution: 'Substitution', intermediate: 'Intermediate calculation', result: 'Result',
    figureTypes: {
      source_crop: 'Source crop', derived_diagram: 'Schematic', theoretical: 'Theoretical values',
      measured: 'Observed values', illustrative: 'Illustration',
    },
  },
};

const htmlTable = (headers, rows, caption, renderCell = escape) =>
  `<div class="table-scroll" tabindex="0" aria-label="${escape(caption)}"><table><caption>${renderCell(caption)}</caption><thead><tr>` +
  headers.map(value => `<th scope="col">${renderCell(value)}</th>`).join('') + '</tr></thead><tbody>' +
  rows.map(row => '<tr>' + row.map(value => `<td>${renderCell(value)}</td>`).join('') + '</tr>').join('') +
  '</tbody></table></div>';

function mdTable(headers, rows, renderCell) {
  const rowText = row => `| ${row.map(value => renderCell(String(value), true)).join(' | ')} |`;
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
  const targets = new Set(content.blocks.map(block => identifier(block.id)));
  const hasSteps = block => block.type === 'steps' || block.type === 'procedure';
  const operations = block => hasSteps(block) ? block.steps.filter(step => step.type === 'operation') : [];
  for (const step of content.blocks.flatMap(operations)) targets.add(identifier(step.id));
  const numbered = new Map();
  for (const block of content.blocks) {
    const entries = block.type === 'equation' ? [block] : operations(block);
    for (const entry of entries.filter(item => item.numbered)) {
      const id = identifier(entry.id);
      if (numbered.has(id) || entry.type === 'operation' && !entry.expression)
        throw new Error('Invalid numbered equation: ' + id);
      numbered.set(id, numbered.size + 1);
    }
  }
  const navText = text => inline(text, targets, id => escape(equationLink(id, 'text'))).replace(/<\/?a(?:\s[^>]*)?>/gu, '');
  const refPrefix = lang.startsWith('ja') ? '式' : 'Eq.';
  function equationLink(id, mode) {
    if (!numbered.has(id)) throw new Error('Missing numbered equation: ' + id);
    const name = refPrefix + ' (' + numbered.get(id) + ')';
    if (mode === 'text') return name;
    return mode === 'html' ? `<a class="equation-ref" href="#eq-${id}">${name}</a>` : `[${name}](#eq-${id})`;
  }
  const rich = value => inline(value, targets, id => equationLink(id, 'html'));
  function linkedMarkdown(value, table = false) {
    const tokens = /(\\\([^\n]*?\\\)|@eq\([A-Za-z0-9][A-Za-z0-9._-]{0,79}\)|\[[^\[\]\n]+\]\(#block-[A-Za-z0-9][A-Za-z0-9._-]{0,79}\))/gu;
    return value.split(tokens).map(part => {
      if (part.startsWith('@eq(') && /^@eq\([A-Za-z0-9][A-Za-z0-9._-]{0,79}\)$/u.test(part))
        return equationLink(part.slice(4, -1), 'md');
      const link = /^\[([^\[\]\n]+)\]\(#block-([A-Za-z0-9][A-Za-z0-9._-]{0,79})\)$/u.exec(part);
      if (!link) return markdown(part, table);
      if (!targets.has(link[2])) throw new Error('Missing lecture link target: ' + link[2]);
      return '[' + markdown(link[1], table) + '](#block-' + link[2] + ')';
    }).join('');
  }
  function numberHtml(body, id) {
    if (!numbered.has(id)) return body;
    return `<div class="numbered-equation" id="eq-${id}" tabindex="-1">${body}<span class="equation-number">(${numbered.get(id)})</span></div>`;
  }
  const numberMd = id => numbered.has(id) ? `<a id="eq-${id}"></a>\n\n(${numbered.get(id)})\n\n` : '';
  const navigation = lang.startsWith('ja') ? '目次' : 'Contents';
  const returnLabel = lang.startsWith('ja') ? '全体に戻る' : 'Overview';
  const sources = new Map(content.sources.map(source => [sourceIdentifier(source.id), source]));
  const referenceIds = new Set([
    ...content.blocks, ...content.equations, ...content.figures,
    ...content.blocks.filter(hasSteps).flatMap(block => block.steps),
  ].flatMap(item => item.source_refs ?? []));
  const usedSources = [...sources.values()].filter(source => referenceIds.has(source.id));
  const references = work => (work.source_refs ?? []).map(id => {
    const source = sources.get(id);
    if (!source || !words[source.kind]) throw new Error('Unknown source reference ' + id);
    return source;
  });
  // Reading views use one endnote per section. The editable source keeps every
  // block/step binding; the endnote expands to the individual evidence locators.
  const sections = [];
  for (const block of content.blocks) {
    if (!sections.length || block.type === 'heading' && block.level === 2)
      sections.push({ title: block.type === 'heading' ? block.text : content.title, blocks: [], ids: new Set() });
    const section = sections.at(-1);
    section.blocks.push(block);
    const related = [block];
    if (hasSteps(block)) related.push(...block.steps);
    if (block.type === 'equation') related.push(equations.get(block.equation_id));
    if (block.type === 'figure') related.push(figures.get(block.figure_id));
    for (const item of related) for (const source of references(item)) section.ids.add(source.id);
  }
  const headings = content.blocks.flatMap(block => block.type === 'heading' ? [block]
    : operations(block).map(step => ({ id: step.id, level: 3, text: step.title })));
  const navigable = content.output_mode === 'worked_explanation' && headings.length > 1;
  const contents = navigable ? `<nav class="study-nav" aria-label="${navigation}"><details><summary>${navigation}</summary><ol>` +
    headings.map(block => `<li class="level-${block.level}"><a href="#block-${identifier(block.id)}">${navText(block.text)}</a></li>`).join('') +
    '</ol></details></nav>' : '';
  const marker = index => `<sup class="note-mark"><a href="#note-${index}" aria-label="${words.sourceList} ${index}">[${index}]</a></sup>`;
  function endnote(section, index) {
    const records = [...section.ids].map(id => sources.get(id));
    const documents = new Map();
    for (const source of records.filter(s => s.locator?.page != null)) {
      const title = source.title.replace(/\s+p\.\s*\d+$/iu, '');
      const key = JSON.stringify([source.kind, title, source.locator.sha256]);
      if (!documents.has(key)) documents.set(key, { title, pages: new Set() });
      documents.get(key).pages.add(source.locator.page);
    }
    const cited = [...documents.values()].map(doc =>
      doc.title + ' pp. ' + [...doc.pages].sort((a, b) => a - b).join(', '));
    const other = records.filter(s => s.kind !== 'current_lecture' || !documents.size && s.locator?.page == null);
    cited.push(...new Set(other.map(s => words[s.kind] + ' · ' + s.title)));
    const details = records.map(source => {
      const locator = sourceDetails(source, words);
      return `<li><span>${escape(source.id)}</span> · ${escape(source.title)}${locator ? ' · ' + escape(locator) : ''}</li>`;
    }).join('');
    return `<li id="note-${index}"><strong>${rich(section.title)}</strong><p>${escape(cited.join(' / '))}</p>` +
      `<details><summary>${words.sourceList} (${records.length})</summary><ul>${details}</ul></details></li>`;
  }
  const mdReferences = (work, omit = []) => {
    const known = references(work).filter(source => !omit.includes(source.id));
    return known.length ? '\n\n' + words.sourceList + ' ' +
      known.map(source => markdown(words[source.kind] + ' · ' + source.title + ' [' + source.id + ']')).join(' / ') : '';
  };
  const eqHtml = (expression, id, kind = 'math') =>
    `<div class="equation" tabindex="0" role="group" aria-label="${words[kind]} ${escape(id)}">${expression}</div>`;
  const mdMath = (tex, id) => '```math id="' + id + '"\n' + tex + '\n```';
  const eqMeta = (equation, reading = false) => {
    const metadata = [];
    if (equation.assumptions?.length)
      metadata.push([`${words.assumptions}`, equation.assumptions.join(' / ')]);
    if (Object.keys(equation.symbol_table ?? {}).length)
      metadata.push([`${words.symbols}`, Object.entries(equation.symbol_table).map(([symbol, definition]) =>
        (reading ? symbolMarkup(symbol, equation.tex) : symbol) + ' = ' + definition).join(' / ')]);
    return metadata;
  };

  const ja = lang.startsWith('ja');
  const stepLabel = n => ja ? '手順' + n : 'Step ' + n;
  const whyLabel = ja ? '理由を読む' : 'Read the explanation';
  const returnTo = n => ja ? stepLabel(n) + 'に戻る' : 'Return to step ' + n;
  const reasonsLabel = ja ? '手順の解説' : 'Explanations';

  function figureHtml(figure, mode, id) {
    const src = mode === 'standalone' ? figure.dataUri : figure.path;
    const data = figure.data ? `<details><summary>${words.values}</summary>` +
      htmlTable(figure.data.headers, figure.data.rows, words.values) + '</details>' : '';
    const kind = words.figureTypes[figure.factuality] ?? '';
    return `<figure id="${id}"><div class="figure-scroll" tabindex="0" aria-label="${escape(figure.alt)}">` +
      `<img src="${src}" alt="${escape(figure.alt)}" width="${figure.width}" height="${figure.height}">\n</div>` +
      `<figcaption>${rich(figure.caption)}${kind ? '（' + kind + '）' : ''}</figcaption>${data}</figure>`;
  }

  function explanationsHtml(steps) {
    return `<section class="procedure-reasons"><h3>${reasonsLabel}</h3>` +
      steps.map((step, i) => `<section id="why-${identifier(step.id)}" tabindex="-1"><h4>${stepLabel(i + 1)}　${rich(step.title)}</h4>` +
        `<p class="prose">${rich(step.explanation)}</p><a href="#block-${step.id}">${returnTo(i + 1)}</a></section>`).join('') + '</section>';
  }

  function stepHtml(block, step, mode) {
    const parts = stepEquations.get(block.id).get(step.id);
    if (step.type === 'operation') {
      const expression = parts.get('expression');
      const back = navigable ? `<a class="back" href="#overview">${returnLabel}</a>` : '';
      const guided = block.type === 'procedure';
      const stage = guided && step.figure_id ? figureHtml(figures.get(step.figure_id), mode, 'stage-' + step.id) : '';
      const why = guided ? `<a class="step-reason-link" href="#why-${identifier(step.id)}">${whyLabel}</a>` : '';
      return `<li id="block-${identifier(step.id)}"${guided ? ' tabindex="-1"' : ''}><p class="step-title">${rich(step.title)}${back}</p>` +
        stage + `<p class="prose">${rich(step.rationale)}</p>` + (expression ? numberHtml(eqHtml(expression, step.id), step.id) : '') + why + '</li>';
    }
    const numeric = [...parts.entries()].map(([kind, expression]) => {
      const equation = eqHtml(expression, step.id, kind);
      return content.output_mode === 'submission_answer'
        ? `<div class="step-equation"><p>${words[kind]}</p>${equation}</div>` : equation;
    }).join('');
    return `<li id="${block.id}-${step.id}"><p class="step-title">${rich(step.title)}</p><p>${rich(step.rationale)}</p>` +
      (content.output_mode === 'submission_answer' ? numeric : `<div class="calculation">${numeric}</div>`) + '</li>';
  }

  function html(block, mode, note) {
    const id = identifier(block.id);
    switch (block.type) {
      case 'heading': {
        const level = Math.min(6, Math.max(2, block.level));
        if (!Number.isInteger(level)) throw new Error('Invalid heading level');
        const back = navigable && level <= 3 ? `<a class="back" href="#overview">${returnLabel}</a>` : '';
        return `<h${level} id="block-${id}">${rich(block.text)}${note}${back}</h${level}>`;
      }
      case 'bullets': return `<section id="block-${id}" class="bullet-group"><p><strong>${rich(block.title)}</strong></p>` +
        `<ul>${block.items.map(item => `<li>${rich(item)}</li>`).join('')}</ul></section>`;
      case 'comparison': return `<section id="block-${id}" class="article-comparison"><p><strong>${rich(block.title)}</strong></p>` +
        '<div class="article-comparison__cases">' + block.cases.map(entry =>
          '<section class="article-comparison__case"><strong class="article-comparison__case-title">' +
          rich(entry.label) + '</strong><div class="article-comparison__case-body"><ul>' +
          entry.items.map(item => `<li>${rich(item)}</li>`).join('') + '</ul></div></section>'
        ).join('') + '</div></section>';
      case 'background': {
        const optional = lang.startsWith('ja') ? '補足' : 'Background';
        const label = `<span class="background-label">${optional}</span> ${rich(block.title)}`;
        const body = `<div class="background-body">${block.paragraphs.map(text => `<p class="prose">${rich(text)}</p>`).join('')}</div>`;
        // A closed native disclosure can suppress its children in WebKit print.
        return `<section class="background" id="block-${id}"><details><summary>${label}</summary>${body}</details>` +
          `<div class="background-print"><p class="background-question">${label}</p>${body}</div></section>`;
      }
      case 'paragraph': return `<p id="block-${id}" class="prose">${rich(block.text)}</p>`;
      case 'callout': return `<aside id="block-${id}"><p><strong>${rich(block.title)}</strong></p><p class="prose">${rich(block.text)}</p></aside>`;
      case 'table': return `<section id="block-${id}">${htmlTable(block.headers, block.rows, block.caption, rich)}</section>`;
      case 'equation': {
        const equation = equations.get(block.equation_id);
        if (!equation) throw new Error('Unknown equation ' + block.equation_id);
        return `<div id="block-${id}" class="equation-block">${numberHtml(eqHtml(equation.expression, equation.id), id)}` +
          eqMeta(equation, true).map(([name, value]) => `<p class="equation-meta"><strong>${name}</strong> ${rich(value)}</p>`).join('') + '</div>';
      }
      case 'figure': {
        const figure = figures.get(block.figure_id);
        if (!figure) throw new Error('Unknown figure ' + block.figure_id);
        return figureHtml(figure, mode, 'block-' + id);
      }
      case 'procedure':
      case 'steps': return `<ol class="steps" id="block-${id}">${block.steps.map(step => stepHtml(block, step, mode)).join('')}</ol>` +
        (block.type === 'procedure' ? explanationsHtml(block.steps) : '');
      default: throw new Error('Unknown lecture block ' + block.type);
    }
  }

  function mdFigure(figure, mode) {
    return mode === 'mdx'
      ? `<Figure assetId="${figure.id}" src="${figure.path}" alt="${escape(figure.alt)}" caption="${escape(figure.caption)}" />`
      : `![${linkedMarkdown(figure.alt)}](${figure.path})\n\n${linkedMarkdown(figure.caption)}`;
  }

  function md(block, mode) {
    const refs = mdReferences(block);
    switch (block.type) {
      case 'heading':
        return '#'.repeat(Math.max(2, block.level)) + ' ' + linkedMarkdown(block.text) + refs;
      case 'bullets': return '**' + linkedMarkdown(block.title) + '**\n\n' +
        block.items.map(item => '- ' + linkedMarkdown(item)).join('\n') + refs;
      case 'comparison': {
        const cases = block.cases.map(entry => {
          const items = entry.items.map(item => '- ' + linkedMarkdown(item)).join('\n');
          return mode === 'mdx'
            ? `<ComparisonCase label="${escape(entry.label)}">\n\n${items}\n\n</ComparisonCase>`
            : '**' + linkedMarkdown(entry.label) + '**\n\n' + items;
        }).join('\n\n');
        return '**' + linkedMarkdown(block.title) + '**\n\n' +
          (mode === 'mdx' ? '<Comparison>\n\n' + cases + '\n\n</Comparison>' : cases) + refs;
      }
      case 'background': {
        const body = block.paragraphs.map(text => linkedMarkdown(text)).join('\n\n');
        return mode === 'mdx' ? `<Background title="${escape(block.title)}">\n\n${body}${refs}\n\n</Background>`
          : '**' + linkedMarkdown(block.title) + '**\n\n' + body + refs;
      }
      case 'paragraph': return linkedMarkdown(block.text) + refs;
      case 'callout': return mode === 'mdx'
        ? `<Callout title="${escape(block.title)}">\n\n${linkedMarkdown(block.text)}${refs}\n\n</Callout>`
        : '> **' + linkedMarkdown(block.title) + '**\n>\n' + linkedMarkdown(block.text).replaceAll('\n', '\n> ') + refs;
      case 'table':
        return '**' + linkedMarkdown(block.caption) + '**\n\n' + mdTable(block.headers, block.rows, linkedMarkdown) + refs;
      case 'equation': {
        const equation = equations.get(block.equation_id);
        return numberMd(block.id) + mdMath(equation.tex, equation.id) + eqMeta(equation).map(([name, value]) =>
          `\n\n**${name}** ${linkedMarkdown(value)}`).join('') + mdReferences(equation) + mdReferences(block, equation.source_refs);
      }
      case 'figure': {
        const figure = figures.get(block.figure_id);
        const view = mdFigure(figure, mode);
        return view + mdReferences(figure) + mdReferences(block, figure.source_refs) +
          (figure.data ? `\n\n[${words.values}](data/${figure.id}.csv)` : '');
      }
      case 'procedure':
      case 'steps': {
        const guided = block.type === 'procedure';
        const steps = block.steps.map((step, index) => {
          const parts = step.type === 'operation' ? (step.expression ? ['expression'] : []) : ['symbolic', 'substitution', 'intermediate', 'result'].filter(kind => step[kind] != null);
          let body = [linkedMarkdown(step.rationale), ...parts.map(kind =>
            (kind === 'expression' ? numberMd(step.id) : '**' + words[kind] + '**\n\n') + mdMath(step[kind], `${block.id}-${step.id}-${kind}`))].join('\n\n') + mdReferences(step);
          if (guided) {
            const anchor = identifier(step.id);
            const figure = step.figure_id ? figures.get(step.figure_id) : null;
            const stage = figure ? mdFigure(figure, mode) + mdReferences(figure) +
              (figure.data ? `\n\n[${words.values}](data/${figure.id}.csv)` : '') + '\n\n' : '';
            body = `<a id="block-${anchor}"></a>\n\n` + stage + body + `\n\n[${whyLabel}](#why-${anchor})`;
          }
          if (!guided && step.type === 'operation') body = `<a id="block-${identifier(step.id)}"></a>\n\n` + body;
          return mode === 'mdx' ? `<Step title="${escape(step.title)}">\n\n${body}\n\n</Step>`
            : `${index + 1}. **${linkedMarkdown(step.title)}**\n\n` + body.split('\n').map(line => line ? '   ' + line : '').join('\n');
        }).join('\n\n');
        const reasons = guided ? '\n\n### ' + reasonsLabel + '\n\n' + block.steps.map((step, i) =>
          `<a id="why-${identifier(step.id)}"></a>\n\n#### ${stepLabel(i + 1)}\n\n${linkedMarkdown(step.explanation)}\n\n[${returnTo(i + 1)}](#block-${step.id})`).join('\n\n') : '';
        return (mode === 'mdx' ? '<Steps>\n\n' + steps + '\n\n</Steps>' : steps) + reasons + refs;
      }
      default: throw new Error('Unknown lecture block ' + block.type);
    }
  }

  function renderHtml(mode, css) {
    const list = sections.map((section, i) => endnote(section, i + 1)).join('');
    const introNote = sections.length && sections[0].blocks[0].type !== 'heading' ? marker(1) : '';
    const style = mode === 'standalone' ? `<style>${css}</style>` : '<link rel="stylesheet" href="lesson.css">';
    const images = mode === 'standalone' ? 'data:' : "'self'";
    const styles = mode === 'standalone' ? "'unsafe-inline'" : "'self'";
    const policy = `default-src 'none'; style-src ${styles}; img-src ${images}; base-uri 'none'; form-action 'none'`;
    return `<!doctype html>\n<html lang="${lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><meta http-equiv="Content-Security-Policy" content="${policy}"><title>${prose(content.title)}</title>${style}</head>\n<body><main id="overview"><h1>${prose(content.title)}${introNote}</h1>${contents}\n` +
      sections.flatMap((section, i) => section.blocks.map((block, j) =>
        html(block, mode, j === 0 && block.type === 'heading' ? marker(i + 1) : ''))).join('\n') +
      `\n<section class="sources"><h2>${words.sourceList}</h2><ol>${list}</ol></section></main></body></html>\n`;
  }

  function renderMarkdown(mode) {
    const list = usedSources.map(source => {
      const details = sourceDetails(source, words);
      return `- ${markdown(source.id + ' · ' + words[source.kind] + ' · ' + source.title + (details ? ' · ' + details : ''))}`;
    }).join('\n');
    const blocks = content.blocks.map(block =>
      (mode === 'mdx' ? `{/* vep:block ${identifier(block.id)} */}\n\n` : '') +
      `<a id="block-${identifier(block.id)}"></a>\n\n` + md(block, mode));
    return `# ${markdown(content.title)}\n\n` + blocks.join('\n\n') +
      (mode === 'mdx' ? '\n\n{/* vep:sources */}' : '') + `\n\n## ${words.sourceList}\n\n${list}\n`;
  }
  return { renderHtml, renderMarkdown };
}
