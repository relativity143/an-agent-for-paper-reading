# 第三方库

这里的文件是原样拷贝的第三方库，不要手改。页面从这里加载，不依赖外网。

| 目录 | 库 | 版本 | 许可 | 来源 |
| --- | --- | --- | --- | --- |
| `katex/` | [KaTeX](https://katex.org/)，公式渲染 | 0.16.9 | MIT，见 `katex/LICENSE` | npm 包 `katex@0.16.9` 的 `dist/` |

`katex/` 里只放了 `katex.min.js`、`katex.min.css` 和 `fonts/` 下的 woff2 字体。样式表里还列着 woff 和 ttf 作为后备，现在的浏览器都用 woff2，不会去取另外两种。

## 核对

拷贝时核对过两点：npm 压缩包的 sha512 与 npm 登记的一致；`katex.min.js` 和 `katex.min.css` 与 cdnjs 上同一版本的文件逐字节相同。

```text
dc84b296ec3e884de093158f760fd9d45b6c7abe58b5381557f4e138f46a58ae  katex/katex.min.js
505d5f829022bb7b4f24dfee0aa1141cd7bba67afe411d1240335f820960b5c3  katex/katex.min.css
```

用 `shasum -a 256 web/vendor/katex/katex.min.js web/vendor/katex/katex.min.css` 可以重新核对。

## 升级

下载新版本的 npm 包，按上面的方法核对，替换这三样文件，更新本表的版本和校验值，再在页面上看一遍公式。
