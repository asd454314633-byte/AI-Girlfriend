/* 自检 png_probe：解码现有四张 PNG，报尺寸/色域/关键区域占比。
 * 目的有两个：(1) 证明这个手写解码器本身没坏（过滤器重建是否正确，
 * 看亮度分布是否合理、有没有出现"半张图错位"的典型特征）；
 * (2) 留档上一批截图到底是哪块 surface。 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const P = require('./png_probe.cjs');

const SHOT = path.join(__dirname, '..', '..', 'evidence', 'UI_DUAL_VIEWMODE_01', 'SCREENSHOTS');
for (const f of fs.readdirSync(SHOT).filter((x) => x.endsWith('.png'))) {
  const buf = fs.readFileSync(path.join(SHOT, f));
  try {
    const img = P.decode(buf);
    console.log(f, {
      sha: P.sha(buf), bytes: buf.length,
      w: img.width, h: img.height, ch: img.channels,
      colorType: img.ihdr.color, interlace: img.ihdr.interlace,
      chunks: img.chunks.join(','),
      lum: P.luminanceSpread(img),
      cornerTL: P.pixel(img, 2, 2), center: P.pixel(img, Math.floor(img.width / 2), Math.floor(img.height / 2)),
    });
  } catch (e) {
    console.log(f, 'DECODE_FAIL', e.message);
  }
}
