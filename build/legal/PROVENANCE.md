# Internal image attribution, not legal certification

Tokenizers 0.22.2 upstream tag resolves to immutable commit
`f383101a26663708484cac0727792aad74f78234` (GitHub public git-ref API,
verified 2026-11-06). Preserved verbatim:

- https://raw.githubusercontent.com/huggingface/tokenizers/f383101a26663708484cac0727792aad74f78234/LICENSE
  -> tokenizers-LICENSE, SHA256 c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4
- https://raw.githubusercontent.com/huggingface/tokenizers/f383101a26663708484cac0727792aad74f78234/tokenizers/LICENSE
  -> tokenizers-rust-LICENSE, SHA256 1e881ecf2862d01e6e5bc2b861e46886d2a6fb01499c0c508a209a7271b13cf2

The recursive public tree API for this commit contains no NOTICE file.
No NOTICE was invented. Missing wheel license entries do not imply lack of a
license. Unused Node bindings and WASM example license files are not runtime
components copied into this image. These upstream licenses cover attribution
for the pinned tokenizer source; this is not certification of all wheel/base
image transitive dependencies or third-party distribution rights.

Model attribution: RedHatAI/Qwen3.8-27B-INT4, revision
91bd022d5b49442a868bc35008f6c21e1860edfa, model card declares Apache-2.0;
the preserved MODEL-README.md and asset README.md retain that declaration.
The canonical Apache license is preserved at ../LICENSE-2.0.txt (official
https://www.apache.org/licenses/LICENSE-2.0.txt; SHA256
cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30).
No model weights are copied. This source-only internal SPI image is not a
third-party redistribution. Validation-only Node/git are not runtime artifacts.
