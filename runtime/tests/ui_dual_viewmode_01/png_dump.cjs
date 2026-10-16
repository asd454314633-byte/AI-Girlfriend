/* png_dump.cjs — 把 PNG 解码成裸 RGB 文件，交给独立的第二引擎（PIL）对账。
 * 用法：node png_dump.cjs <in.png> <out.rgb>
 *  Sidecar <out.rgb.json> 记录宽高/通道数/源文件 sha，供 Python 侧核对。 */
'use strict';
const fs = require('node:fs');
const P = require('./png_probe.cjs');

const [inF, outF] = process.argv.slice(2);
if (!inF || !outF) { console.error('usage: node png_dump.cjs <in.png> <out.rgb>'); process.exit(2); }
const buf = fs.readFileSync(inF);
const img = P.decode(buf);
// 统一成 3 通道，方便与 PIL 的 RGB 逐字节比较
const rgb = Buffer.allocUnsafe(img.width * img.height * 3);
if (img.channels === 3) img.data.copy(rgb);
else if (img.channels === 4) {
  for (let i = 0, j = 0; i < img.data.length; i += 4, j += 3) {
    rgb[j] = img.data[i]; rgb[j + 1] = img.data[i + 1]; rgb[j + 2] = img.data[i + 2];
  }
} else {
  for (let i = 0, j = 0; i < img.data.length; i += img.channels, j += 3) {
    rgb[j] = rgb[j + 1] = rgb[j + 2] = img.data[i];
  }
}
fs.writeFileSync(outF, rgb);
fs.writeFileSync(outF + '.json', JSON.stringify({
  source: inF, bytes: buf.length, sha16: P.sha(buf),
  width: img.width, height: img.height, channels_decoded: img.channels,
  colorType: img.ihdr.color, depth: img.ihdr.depth, interlace: img.ihdr.interlace,
}));
console.log('OK', img.width + 'x' + img.height, 'ch' + img.channels, buf.length + 'B');
