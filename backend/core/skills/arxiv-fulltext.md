---
name: arxiv-fulltext
description: 取 arXiv 论文全文的可行路径(本机 curl 直连 + pdftotext 转文本)
---

# arXiv 全文取法

**背景**: 本环境的 WebFetch 对任何域名都报"Unable to verify if domain is safe"被禁, 但**本机网络能直连 arXiv**(Git Bash 与 WSL 的 curl 均通)。涉及"这篇到底做了什么/它的局限是什么"这类关键判断时,**一律取全文核对**, 不要依赖搜索片段(片段级二手/AI 解读站已多次抓错)。

## 标准流程

```bash
mkdir -p /c/Users/10601/arxiv_pdf && cd /c/Users/10601/arxiv_pdf
curl -sSL -o 2606.11211.pdf "https://arxiv.org/pdf/2606.11211"
pdftotext -q 2606.11211.pdf 2606.11211.txt    # 转纯文本后用 read 工具读
```

- curl 与 pdftotext 都在 Git Bash 的 /mingw64/bin/ 下, bash 里直接可用。
- **优先走 pdftotext 转 .txt 再 Read** —— 不要直接 Read PDF: arXiv 的 PDF 有时被 Read 工具误判为 "password-protected"(即使 pdftotext 能读)。
- 备选: 主域连不上用镜像 `https://export.arxiv.org/pdf/<id>`; 只有 HTML 版的新论文用 `https://arxiv.org/html/<id>`。
- 论文 id 形如 `2606.11211`(YYMM.NNNNN), 来自 arXiv 列表页/引用文本; 不确定时先搜确认 id 再下。

## 规矩

- 下载的 pdf/txt 放 /c/Users/10601/arxiv_pdf/(别散落到工作目录)。
- 读完把关键结论凝练进对话/报告, 引原文时带上文内章节或页码线索, 别只凭记忆转述。
