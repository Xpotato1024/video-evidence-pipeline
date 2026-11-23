import { escape, label, number, object } from './common.mjs';

const elementFields = {
  line: ['x1', 'y1', 'x2', 'y2'],
  arrow: ['x1', 'y1', 'x2', 'y2'],
  circle: ['cx', 'cy', 'r'],
  ellipse: ['cx', 'cy', 'rx', 'ry'],
  rect: ['x', 'y', 'width', 'height'],
  polyline: ['points'],
  polygon: ['points'],
  text: ['x', 'y', 'text', 'anchor', 'font_size'],
  arc: ['cx', 'cy', 'r', 'start', 'end'],
};

function paint(value, fallback) {
  const color = value ?? fallback;
  if (typeof color !== 'string' || !/^(#[\da-f]{3}|#[\da-f]{6}|[a-z]{1,20})$/i.test(color))
    throw new Error('Use an SVG color keyword or hex triplet');
  return color;
}

export function geometrySvg(raw, description) {
  const spec = object(raw, ['width', 'height', 'items', 'physical_model']);
  const width = number(spec.width, 100, 1200);
  const height = number(spec.height, 80, 1200);
  if (!Array.isArray(spec.items) || spec.items.length < 1 || spec.items.length > 160)
    throw new Error('Geometry needs 1–160 drawing items');
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img"><desc>${escape(description)}</desc>` +
    spec.items.map(item => renderItem(item, width, height)).join('') + '</svg>';
}

function renderItem(item, width, height) {
  const fields = Object.hasOwn(elementFields, item?.kind) && elementFields[item.kind];
  if (!fields) throw new Error('Unknown geometry item');
  object(item, ['kind', 'stroke', 'stroke_width', 'fill', 'dash', ...fields]);
  const x = value => number(value, 0, width);
  const y = value => number(value, 0, height);
  const radius = value => number(value, 0.01, Math.max(width, height));
  if (item.dash !== undefined && typeof item.dash !== 'boolean')
    throw new Error('dash must be boolean');
  const stroke = paint(item.stroke, '#253b52');
  const weight = number(item.stroke_width ?? 2, 0.1, 12);
  const fill = paint(item.fill, 'none');
  const style = `stroke="${stroke}" stroke-width="${weight}" fill="${fill}" stroke-linejoin="round" stroke-linecap="round"${item.dash ? ' stroke-dasharray="6 4"' : ''}`;
  switch (item.kind) {
    case 'line':
    case 'arrow': {
      const x1 = x(item.x1), y1 = y(item.y1), x2 = x(item.x2), y2 = y(item.y2);
      const line = `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" ${style}/>`;
      if (item.kind === 'line') return line;
      const length = Math.hypot(x2 - x1, y2 - y1);
      if (length < 3) throw new Error('An arrow needs distinct start and end points');
      const headLength = Math.min(9 + weight, length / 2);
      const ux = (x2 - x1) / length, uy = (y2 - y1) / length;
      const px = x2 - ux * headLength, py = y2 - uy * headLength;
      const offsetX = uy * headLength * 0.42, offsetY = ux * headLength * 0.42;
      return line + `<polygon points="${x2},${y2} ${px + offsetX},${py - offsetY} ${px - offsetX},${py + offsetY}" fill="${stroke}"/>`;
    }
    case 'circle':
      return `<circle cx="${x(item.cx)}" cy="${y(item.cy)}" r="${radius(item.r)}" ${style}/>`;
    case 'ellipse':
      return `<ellipse cx="${x(item.cx)}" cy="${y(item.cy)}" rx="${radius(item.rx)}" ry="${radius(item.ry)}" ${style}/>`;
    case 'rect':
      return `<rect x="${x(item.x)}" y="${y(item.y)}" width="${number(item.width, 1, width - item.x)}" height="${number(item.height, 1, height - item.y)}" ${style}/>`;
    case 'polyline':
    case 'polygon': {
      const minimum = item.kind === 'polyline' ? 2 : 3;
      if (!Array.isArray(item.points) || item.points.length < minimum || item.points.length > 500)
        throw new Error('Geometry contains an invalid number of points');
      const points = item.points.map(point => {
        if (!Array.isArray(point) || point.length !== 2) throw new Error('Expected a point pair');
        return `${x(point[0])},${y(point[1])}`;
      });
      return `<${item.kind} points="${points.join(' ')}" ${style}/>`;
    }
    case 'text': {
      const anchor = item.anchor ?? 'start';
      if (!['start', 'middle', 'end'].includes(anchor)) throw new Error('Unknown text anchor');
      const size = number(item.font_size ?? 17, 12, 34);
      return `<text x="${x(item.x)}" y="${y(item.y)}" font-family="Noto Sans CJK JP,sans-serif" font-size="${size}" text-anchor="${anchor}" fill="${paint(item.fill, '#142632')}">${escape(label(item.text))}</text>`;
    }
    case 'arc': {
      const cx = x(item.cx), cy = y(item.cy), r = radius(item.r);
      const start = number(item.start, -360, 360);
      const end = number(item.end, -360, 360);
      const rotation = end - start;
      if (!rotation || Math.abs(rotation) >= 360) throw new Error('An arc needs a nonzero sweep under 360°');
      const point = angle => {
        const radians = angle * Math.PI / 180;
        return `${cx + Math.cos(radians) * r} ${cy + Math.sin(radians) * r}`;
      };
      return `<path d="M ${point(start)} A ${r} ${r} 0 ${Number(Math.abs(rotation) > 180)} ${Number(rotation > 0)} ${point(end)}" ${style}/>`;
    }
    default:
      throw new Error('Unknown geometry item');
  }
}
