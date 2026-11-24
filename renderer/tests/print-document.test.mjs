import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const exec = promisify(execFile);
const printer = fileURLToPath(new URL('../print-document.mjs', import.meta.url));

test('generic document printer creates a tagged A4 PDF without replacing an existing output', async () => {
  const root = await mkdtemp(join(tmpdir(), 'vep-document-print-'));
  const input = join(root, 'source.html');
  const output = join(root, 'document.pdf');
  const print = () => exec(process.execPath, [printer, input, output], { timeout: 45_000 });
  try {
    await writeFile(input,
      '<!doctype html><meta charset="utf-8"><title>Synthetic minutes</title>' +
      '<h1>Minutes</h1><p>Action owner unresolved.</p>');
    await print();
    const bytes = await readFile(output);
    const pdf = bytes.toString('latin1');
    assert.ok(pdf.startsWith('%PDF-'));
    assert.match(pdf, /\/CreationDate\s*\(D:19700101000000/u);
    assert.match(pdf, /\/StructTreeRoot\b/u);
    const mediaBox = /\/MediaBox\s*\[0\s+0\s+([\d.]+)\s+([\d.]+)\]/u.exec(pdf);
    assert.ok(mediaBox);
    assert.ok(Math.abs(Number(mediaBox[1]) - 595.28) < 3);
    assert.ok(Math.abs(Number(mediaBox[2]) - 841.89) < 3);
    await assert.rejects(print, /EEXIST/u);
    assert.deepEqual(await readFile(output), bytes);
  } finally {
    await rm(root, { force: true, recursive: true });
  }
});
