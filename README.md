# Bleubirddy.github.io

The Bleubirddy hobby site: <https://bleubirddy.github.io/>

Plain HTML, CSS and JavaScript. There is no build step: GitHub Pages serves
the files on `main` as they are, so a push to `main` is a publish.

## Pages

| File | What it is |
|---|---|
| `index.html` | Home: the tools and the apps |
| `tools.html` | The list of tools |
| `about.html` | What the site is and how it treats files |
| `404.html` | Shown by GitHub Pages for any address that doesn't exist |
| `styles.css` | Shared styles for the four pages above |
| `favicon.svg` | The tab icon |

## Tools

Each tool lives in its own folder under `tools/` and carries its own styles
and scripts, so it works on its own.

| Folder | What it does |
|---|---|
| `tools/comic-to-epub/` | CBZ, CBR and PDF comics to EPUB, with lettering clean-up |
| `tools/comic-yassifier/` | CBZ to `.xtch` for the Xteink X3 |
| `tools/sleep-screen-maker/` | Images to e-reader sleep screens |

`tools/comic_convert.py` is the command-line version of Comic to EPUB. No
page links to it.

## Links

Links between pages start with `/` (for example `/tools.html`). The 404 page
can show up at any folder depth, and a link without the `/` would break there.

## Trying a change before pushing

```bash
python3 -m http.server 8000
```

Then open <http://localhost:8000/>.
