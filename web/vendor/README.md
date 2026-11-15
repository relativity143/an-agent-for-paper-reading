# 第三方库

这里的文件是原样拷贝的第三方库，不要手改。页面从这里加载，不依赖外网。

| 目录 | 库 | 版本 | 许可 | 来源 |
| --- | --- | --- | --- | --- |
| `katex/` | [KaTeX](https://katex.org/)，公式渲染 | 0.16.9 | MIT，见 `katex/LICENSE` | npm 包 `katex@0.16.9` 的 `dist/` |
| `pdfjs/` | [PDF.js](https://mozilla.github.io/pdf.js/)，在页面里显示 PDF | 6.5.438 | Apache-2.0，见 `pdfjs/LICENSE` | npm 包 `pdfjs-dist@6.5.438` 的 `build/` 和 `web/pdf_viewer.css` |

`katex/` 里只放了 `katex.min.js`、`katex.min.css` 和 `fonts/` 下的 woff2 字体。样式表里还列着 woff 和 ttf 作为后备，现在的浏览器都用 woff2，不会去取另外两种。

`pdfjs/` 里放了 `pdf.min.mjs`（显示）、`pdf.worker.min.mjs`（在后台线程里解析文件）和 `text_layer.css`。`text_layer.css` 是从包里的 `web/pdf_viewer.css` 原样截出的 `.textLayer` 一段，只在开头加了一行说明。包里的备用字体（`standard_fonts/`）、字符映射（`cmaps/`）、图像解码用的 wasm 和单独的绘制线程脚本都没有放进来。

## 核对

拷贝时核对过两点：npm 压缩包的 sha512 与 npm 登记的一致；`katex.min.js` 和 `katex.min.css` 与 cdnjs 上同一版本的文件逐字节相同。

```text
dc84b296ec3e884de093158f760fd9d45b6c7abe58b5381557f4e138f46a58ae  katex/katex.min.js
505d5f829022bb7b4f24dfee0aa1141cd7bba67afe411d1240335f820960b5c3  katex/katex.min.css
```

用 `shasum -a 256 web/vendor/katex/katex.min.js web/vendor/katex/katex.min.css` 可以重新核对。

PDF.js：npm 压缩包的 sha512 与 npm 登记的一致；两份脚本是从包里原样拷出的，没有和别的来源对照。

```text
3a88402882cfa3e7e72c2c9b5149f52f0f88baf1c5b62aa11a41abdbc2072440  pdfjs/pdf.min.mjs
1e64c8240e54735ec5913b37acd5134747799cb8c6bb3777c70adbc01232d2aa  pdfjs/pdf.worker.min.mjs
```

## 升级

下载新版本的 npm 包，按上面的方法核对，替换这三样文件，更新本表的版本和校验值，再在页面上看一遍公式。

PDF.js 升级时同样替换两份脚本，并从新版的 `web/pdf_viewer.css` 里重新截出 `.textLayer` 一段；`js/pdf-view.js` 用到的接口（`getDocument`、`page.render`、`TextLayer`）跨大版本可能有变化，升级后要在页面上打开一份 PDF 看一遍。
