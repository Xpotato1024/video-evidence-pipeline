import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { mermaidSource } from '../browser.mjs';
import { renderChart, toCsv } from '../chart.mjs';
import { hash, safeSvg } from '../common.mjs';
import { inline, mathml } from '../document.mjs';
import { geometrySvg } from '../geometry.mjs';

const exec = promisify(execFile);
const fixture = {
  schema_version: '2.0', output_mode: 'worked_explanation', language: 'ja', title: '合成教材の表示試験',
  sources: [
    { id: 'current', kind: 'current_lecture', title: '合成資料', locator: { page: 2, ref_id: 'fixture' } },
    { id: 'supplement', kind: 'supplement', title: '検証用の例', locator: { note: '作図の補足' } },
    { id: 'handout:P0002', kind: 'current_lecture', title: '切り出し試験資料', locator: { page: 2 } },
  ],
  equations: [{ id: 'partial', tex: String.raw`\frac{\partial u_z}{\partial r} = \sum_{i=1}^n q_i`, symbol_table: { r: '距離' }, assumptions: ['定常'], source_refs: ['current'] }],
  blocks: [
    { id: 'heading', type: 'heading', level: 2, text: String.raw`式と図 \(u\)`, source_refs: ['current'] },
    { id: 'basis-note', type: 'background', title: 'なぜ方向を分ける？', paragraphs: ['各軸の向きを使う。', String.raw`変化率を \(q\) と置く。`], source_refs: ['supplement'] },
    { id: 'directions', type: 'bullets', title: '二方向', items: [String.raw`**縦** \(y\)`, '横'], source_refs: ['current'] },
    { id: 'actions', type: 'steps', source_refs: ['current'], steps: [
      { id: 'select', type: 'operation', title: '値を選ぶ', rationale: '等値線の高さを決める。', expression: 'h=1', source_refs: ['current'] },
      { id: 'connect', type: 'operation', title: '点を結ぶ', rationale: '同じ高さの点をつなぐ。', source_refs: ['current'] },
    ] },
    { id: 'construction', type: 'procedure', kind: 'drawing', source_refs: ['supplement'], steps: [
      { type: 'operation', id: 'draw-axes', title: '軸を引く', rationale: '横軸と縦軸を引く。', expression: 'x=0',
        figure_id: 'axes', explanation: 'これが測定の基準になる。', source_refs: ['supplement'] },
      { type: 'operation', id: 'draw-points', title: '測定点を打つ', rationale: '座標ごとに点を打つ。',
        figure_id: 'chart', explanation: '点の位置が測定結果を示す。', source_refs: ['supplement'] },
    ] },
    { id: 'formula', type: 'equation', equation_id: 'partial', source_refs: ['current'] },
    { id: 'curve-block', type: 'figure', figure_id: 'chart', source_refs: ['supplement'] },
    { id: 'diagram-block', type: 'figure', figure_id: 'axes', source_refs: ['supplement'] },
    { id: 'flow-block', type: 'figure', figure_id: 'flow', source_refs: ['supplement'] },
    { id: 'crop-block', type: 'figure', figure_id: 'crop', source_refs: ['handout:P0002'] },
    { id: 'values-heading', type: 'heading', level: 2, text: '測定値', source_refs: ['current'] },
    { id: 'table-block', type: 'table', caption: '値の並び', headers: ['値', '単位'], rows: [['4.5', '1']], source_refs: ['current'] },
    { id: 'steps', type: 'steps', source_refs: ['current'], steps: [
      { id: 'solve', title: '面積を計算する', rationale: '幅と高さの積を用いる。', symbolic: String.raw`A = b h`,
        substitution: 'A = 2 \u00d7 3', intermediate: 'A=6.0000', result: String.raw`A = 6.000\,\mathrm{m}^2`, source_refs: ['current'] },
    ] },
  ],
  figures: [
    {
      id: 'chart', kind: 'vega-lite', alt: '単位なしの曲線', caption: '例示データの静的グラフ', source_refs: ['supplement'],
      spec: { mark: 'line', x: { field: 'r', label: '距離', unit: 'm', domain: [0, 2] },
        y: { field: 'u', label: '速度', unit: 'm/s', domain: [0, 5] }, values: [{ r: 0, u: 5 }, { r: 1, u: 3 }, { r: 2, u: 0 }] },
    },
    {
      id: 'axes', kind: 'geometry', alt: '正の向きを示す二軸', caption: '二つの正方向', source_refs: ['supplement'],
      spec: { width: 360, height: 180, items: [
        { kind: 'arrow', x1: 60, y1: 140, x2: 295, y2: 140 },
        { kind: 'arrow', x1: 60, y1: 140, x2: 60, y2: 35 },
        { kind: 'text', x: 290, y: 164, text: '軸方向' },
      ] },
    },
    {
      id: 'flow', kind: 'mermaid', alt: '測定から比較への流れ', caption: '確認順序', source_refs: ['supplement'],
      spec: { code: 'flowchart TD\naccTitle: 確認順序\naccDescr: 測定から比較までの流れ\na["測定"]\nb["比較"]\na --> b' },
    },
    {
      id: 'crop', kind: 'source_crop', alt: '元画像から切り出した合成画像', caption: '切り出し画像',
      source_refs: ['handout:P0002'], spec: { path: 'source.png', bbox: [1, 1, 2, 2] },
    },
  ],
};

test('KaTeX emits structural math without a raw TeX annotation', () => {
  const math = mathml(String.raw`\vec{v} = \frac{\partial \phi}{\partial x} + \begin{bmatrix} a & b \\ c & d \end{bmatrix}`);
  for (const tag of ['math', 'mover', 'mfrac', 'mtable']) assert.match(math, new RegExp('<' + tag));
  assert.doesNotMatch(math, /annotation|\\frac/);
  assert.throws(() => mathml('x = \theta'), /unsupported/);
  assert.throws(() => mathml(String.raw`\href{https://example.invalid/}{x}`), /unsupported/);
  assert.throws(() => mathml(String.raw`\text{Everything entered as plain text}`), /presentation MathML/);
});

test('reading prose typesets inline math, emphasis and verified fragment links', () => {
  const tree = inline(String.raw`**基底** \(\mathbf e_r\) と [境界](#block-wall)`, new Set(['wall']));
  assert.match(tree, /<strong>基底<\/strong>/u);
  assert.match(tree, /<msub>/u);
  assert.match(tree, /href="#block-wall"/u);
  assert.doesNotMatch(tree, /display="block"|\\mathbf|e_r/u);
  assert.equal(inline('<script>'), '&lt;script&gt;');
  assert.throws(() => inline('[jump](#block-missing)'), /Missing lecture link/u);
  assert.throws(() => inline(String.raw`\(\href{https://invalid}{x}\)`), /unsupported/u);
});

test('chart rejects ambiguous or clipped data and CSV escapes formula cells', async () => {
  const spec = fixture.figures[0].spec;
  const chart = await renderChart(spec);
  assert.match(chart.svg, /距離/);
  assert.deepEqual(chart.rows, [[0, 5], [1, 3], [2, 0]]);
  await assert.rejects(renderChart({ ...spec, x: { ...spec.x, domain: [1, 2] } }), /clip/);
  await assert.rejects(renderChart({ ...spec, transform: [{}] }), /Invalid fields/);
  await assert.rejects(renderChart({ ...spec, mark: 'bar', y: { ...spec.y, domain: [-2, -1] } }), /zero baseline/);
  assert.equal(toCsv(['+label', 'units'], [['=formula', -2]]), '"\'+label","units"\n"\'=formula",-2\n');
});

test('SVG and Mermaid reject non-static elements, external resources and code', () => {
  assert.match(safeSvg(geometrySvg(fixture.figures[1].spec, 'geometry')).svg, /<polygon/);
  const svg = body => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1">${body}</svg>`;
  assert.throws(() => safeSvg(svg('<foreignObject/>')), /Unsupported/);
  assert.throws(() => safeSvg(svg('<use href="https://example.invalid/logo.svg#id"/>')), /Unsafe/);
  assert.throws(() => safeSvg(svg('<style>@import "https://example.invalid/style.css";</style>')), /Unsafe/);
  assert.throws(() => mermaidSource({ code: fixture.figures[2].spec.code + '\nclick a "https://example.invalid"' }), /Unsupported/);
});

test('renderer builds editable, offline and same-origin outputs from matching source', async () => {
  const root = await mkdtemp(join(tmpdir(), 'vep-render-test-'));
  try {
    const input = join(root, 'input.json');
    const output = join(root, 'output');
    const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAHElEQVR4nGNkmLKVAQaYGBgY/jfUQjiM6DJwAACilASVI6/wDAAAAABJRU5ErkJggg==', 'base64');
    await writeFile(join(root, 'source.png'), png);
    await writeFile(input, JSON.stringify(fixture));
    const program = fileURLToPath(new URL('../render.mjs', import.meta.url));
    await exec(process.execPath, [program, '--input', input, '--output', output, '--assets', root], { timeout: 95_000 });
    const [html, md, mdx, site, pdf, receipt] = await Promise.all(
      ['review.html', 'lesson.md', 'lesson.mdx', 'sites/index.html', 'review.pdf', 'render-receipt.json']
        .map(name => readFile(join(output, name))));
    assert.equal((html.toString().match(/<math\b/gu) ?? []).length, 14);
    assert.equal((html.toString().match(/<img src="data:image\/svg\+xml/gu) ?? []).length, 5);
    assert.equal((site.toString().match(/<img src="figures\//gu) ?? []).length, 6);
    assert.match(html.toString(), /<img src="data:image\/png;base64,[^"]+" alt="[^"]+" width="2" height="2"/u);
    assert.deepEqual(await readFile(join(output, 'inputs/source.png')), png);
    const [body, notes] = html.toString().split('<section class="sources">');
    assert.equal((body.match(/href="#note-1"/g) ?? []).length, 1);
    assert.doesNotMatch(body, /<p class="source-link"|>合成資料<|<p>数値代入<\/p>/);
    assert.match(body, /<ul><li><strong>縦<\/strong> <math[\s\S]*?<\/math><\/li><li>横<\/li><\/ul>/u);
    assert.match(body, /href="#block-select"/u);
    assert.match(body, /<li id="block-connect">/u);
    assert.match(mdx.toString(), /<Step title="値を選ぶ">[\s\S]*?```math id="actions-select-expression"/u);
    assert.match(md.toString(), /\n- [^\n]*縦[^\n]*\n- 横/u);
    assert.match(body, /<section class="background" id="block-basis-note"><details><summary>/u);
    assert.match(mdx.toString(), /<Background title="なぜ方向を分ける？">/u);
    assert.match(md.toString(), /\*\*なぜ方向を分ける？\*\*/u);
    const firstStage = fixture.blocks.find(block => block.id === 'construction').steps[0];
    for (const view of [body, site.toString(), md.toString(), mdx.toString()]) {
      const start = view.indexOf('id="block-' + firstStage.id + '"');
      const end = view.indexOf('#why-' + firstStage.id, start);
      assert.ok(start >= 0 && end > start);
      const stepBody = view.slice(start, end);
      const caption = fixture.figures.find(figure => figure.id === firstStage.figure_id).caption;
      assert.ok(stepBody.indexOf(caption) >= 0);
      assert.ok(stepBody.indexOf(firstStage.rationale) > stepBody.indexOf(caption));
    }
    for (const [i, step] of ['draw-axes', 'draw-points'].entries()) {
      assert.match(body, new RegExp(`<li id="block-${step}"[^>]*>(?:(?!</li>)[\\s\\S])*<figure id="stage-${step}"(?:(?!</li>)[\\s\\S])*href="#why-${step}"`));
      assert.match(body, new RegExp(`<section id="why-${step}"[^>]*>(?:(?!</section>)[\\s\\S])*href="#block-${step}">手順${i+1}に戻る`));
      for (const editable of [md, mdx]) {
        for (const id of ['block-' + step, 'why-' + step]) {
          assert.ok(editable.toString().includes('id="' + id + '"'));
          assert.ok(editable.toString().includes('](#' + id + ')'));
        }
      }
    }
    assert.match(body, /class="calculation"/);
    assert.match(body, /<nav class="study-nav"/);
    assert.match(body, /href="#block-values-heading"/);
    assert.match(notes, /id="note-1"/);
    assert.match(notes, /合成資料/);
    assert.match(notes, /handout:P0002/);
    assert.match(mdx.toString(), /<Steps>/);
    assert.match(mdx.toString(), /\{\/\* vep:block steps \*\/\}/u);
    assert.match(mdx.toString(), /```math id="partial"/);
    assert.match(md.toString(), /1\. \*\*面積を計算する\*\*/u);
    assert.match(md.toString(), /\| 値 \| 単位 \|\n\| --- \| --- \|\n\| 4\.5 \| 1 \|/u);
    assert.equal(pdf.subarray(0, 5).toString(), '%PDF-');
    assert.match(pdf.toString('latin1'), /\/FontFile/);
    const evidence = JSON.parse(receipt);
    assert.equal(evidence.content_sha256, hash(await readFile(input)));
    assert.deepEqual(evidence.validation.chromium_viewport_px, [320, 390, 768, 1440]);
    for (const [path, record] of Object.entries(evidence.files)) {
      const bytes = await readFile(join(output, path));
      assert.equal(record.sha256, hash(bytes));
      assert.equal(record.size_bytes, bytes.length);
    }
    if (process.env.VEP_LAYOUT_QA === '1') {
      const layoutCheck = fileURLToPath(new URL('../check-layout.mjs', import.meta.url));
      const { stdout } = await exec(process.execPath, [layoutCheck, output, join(root, 'layout')], { timeout: 150_000 });
      const results = JSON.parse(stdout).results;
      assert.equal(results.length, 26);
      assert.ok(results.every(result => result.backgroundCount === 1));
      assert.deepEqual(new Set(results.map(result => result.browser)), new Set(['chromium', 'webkit']));
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
