import { compile } from 'vega-lite';
import { View, parse } from 'vega';
import { identifier, label, number, object } from './common.mjs';

function axisDefinition(raw) {
  const axis = object(raw, ['field', 'label', 'unit', 'domain', 'type']);
  identifier(axis.field);
  const type = axis.type ?? 'quantitative';
  if (!['quantitative', 'nominal'].includes(type)) throw new Error('Unsupported chart axis type');
  const title = label(axis.label, 64) + ' [' + label(axis.unit, 24) + ']';
  if (axis.domain !== undefined && (type !== 'quantitative' || !Array.isArray(axis.domain) ||
      axis.domain.length !== 2 || !axis.domain.every(Number.isFinite) || axis.domain[0] >= axis.domain[1]))
    throw new Error('Invalid quantitative axis domain');
  return { ...axis, title, type };
}

function readValue(value, axis) {
  if (axis.type === 'nominal') return label(value, 64);
  const numeric = number(value, -1e100, 1e100);
  if (axis.domain && (numeric < axis.domain[0] || numeric > axis.domain[1]))
    throw new Error('Axis domain would clip the data');
  return numeric;
}

export async function renderChart(raw) {
  const spec = object(raw, ['mark', 'values', 'x', 'y', 'series', 'calculation']);
  if (!['line', 'point', 'bar'].includes(spec.mark)) throw new Error('Unsupported chart mark');
  const x = axisDefinition(spec.x);
  const y = axisDefinition(spec.y);
  const series = spec.series === undefined ? null : object(spec.series, ['field', 'label']);
  if (series) {
    identifier(series.field);
    label(series.label, 64);
  }
  const fields = series ? [x.field, y.field, series.field] : [x.field, y.field];
  if (new Set(fields).size !== fields.length || y.type !== 'quantitative' ||
      (x.type !== 'quantitative' && spec.mark === 'line'))
    throw new Error('Unsupported combination of chart axes and mark');
  if (!Array.isArray(spec.values) || spec.values.length < 1 || spec.values.length > 500)
    throw new Error('Chart needs 1–500 inline rows');
  if (spec.mark === 'bar' && y.domain && (y.domain[0] > 0 || y.domain[1] < 0))
    throw new Error('Bar chart must show its zero baseline');
  const values = spec.values.map(row => {
    object(row, fields);
    const pair = { x: readValue(row[x.field], x), y: readValue(row[y.field], y) };
    return series ? { ...pair, series: label(row[series.field], 64) } : pair;
  });
  const encode = (axis, field, zero) => ({
    field, type: axis.type, title: axis.title,
    ...(axis.type === 'quantitative' ? { scale: { zero, ...(axis.domain ? { domain: axis.domain, nice: false } : {}) } } : {}),
  });
  const encoding = { x: encode(x, 'x', false), y: encode(y, 'y', spec.mark === 'bar') };
  if (series) {
    encoding.color = { field: 'series', type: 'nominal', title: series.label, legend: { orient: 'bottom' } };
    if (spec.mark === 'line') encoding.strokeDash = { field: 'series', title: series.label };
    if (spec.mark === 'point') encoding.shape = { field: 'series', type: 'nominal', title: series.label };
    if (spec.mark === 'bar' && x.type === 'nominal') {
      encoding.xOffset = { field: 'series', type: 'nominal' };
      encoding.y.stack = null;
    }
  }
  const compiled = compile({
    width: 340, height: 215, data: { values },
    mark: { type: spec.mark, color: '#096774', ...(spec.mark === 'line' ? { strokeWidth: 2.8 } : {}) },
    encoding,
    config: {
      font: 'Noto Sans CJK JP, sans-serif',
      axis: { labelFontSize: 13, titleFontSize: 14, labelLimit: 160, titleLimit: 280 },
      legend: { labelFontSize: 13, titleFontSize: 13, labelLimit: 170 },
      view: { stroke: null },
    },
  });
  const noExternalData = { load() { throw new Error('External Vega data is disabled'); } };
  const view = new View(parse(compiled.spec), { renderer: 'none', loader: noExternalData });
  let svg;
  try {
    svg = await view.toSVG();
  } finally {
    view.finalize();
  }
  const headers = series ? [x.title, y.title, series.label] : [x.title, y.title];
  const rows = values.map(row => series ? [row.x, row.y, row.series] : [row.x, row.y]);
  return { svg, headers, rows };
}

export function toCsv(headers, rows) {
  const cell = value => {
    if (typeof value === 'number') return String(value);
    const text = /^[\t\r\n]|^\s*[=+@-]/u.test(value) ? "'" + value : value;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  return [headers, ...rows].map(row => row.map(cell).join(',')).join('\n') + '\n';
}
