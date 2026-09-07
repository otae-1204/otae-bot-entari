# Upstream attribution

`assets/system_prompt.txt` and `assets/card.html` originate from
[kumoSleeping/entari-plugin-hyw](https://github.com/kumoSleeping/entari-plugin-hyw/tree/0ca5b645ba63de5f637be4df2358d55aeeaaa17d),
commit `0ca5b645ba63de5f637be4df2358d55aeeaaa17d` (plugin version 4.0.11).
The upstream README and package metadata declare the MIT license.

The system prompt is extracted from `core/policies.py`; only the dynamic current
time is replaced at runtime. Local tool and output constraints are appended in
`agent.py`. The bundled card is copied from
`core/tools/_public/browser/assets/card-dist/index.html`; local rendering injects
data, a network-restricting CSP and a style hiding unavailable remote favicons
without changing the vendored file. Third-party runtime notices are retained in
`assets/THIRD_PARTY_LICENSES.txt`.
The Python integration is implemented for this repository's Entari version and
shared browser/image executors; it does not install the upstream distribution.

Copyright (c) kumoSleeping and entari-plugin-hyw contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
