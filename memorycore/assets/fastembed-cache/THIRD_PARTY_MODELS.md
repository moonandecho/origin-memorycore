# Bundled embedding models — license notice

This directory contains two ONNX embedding models, both distributed under the
**MIT License**. They are copied verbatim from their upstream sources so that
MemoryCore works fully offline — no download is ever required.

## BAAI/bge-small-zh-v1.5 (default, 512-dim, Chinese)

- Copyright (c) Beijing Academy of Artificial Intelligence (BAAI)
- Source model: https://huggingface.co/BAAI/bge-small-zh-v1.5
- ONNX conversion used by fastembed: https://huggingface.co/Qdrant/bge-small-zh-v1.5
- License: MIT

## BAAI/bge-small-en-v1.5 (optional, 384-dim, English)

- Copyright (c) Beijing Academy of Artificial Intelligence (BAAI)
- Source model: https://huggingface.co/BAAI/bge-small-en-v1.5
- ONNX conversion used by fastembed: https://huggingface.co/qdrant/bge-small-en-v1.5-onnx-q
- License: MIT

## MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
