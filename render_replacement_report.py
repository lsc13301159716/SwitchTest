"""Use the canonical DOCX PNG renderer with an already exported native Word PDF.

Windows dependencies do not include LibreOffice. No system LibreOffice is used.
"""
import importlib.util
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parent
QA=ROOT/'out/replacement_analysis_20261026/qa'
DEPS=Path(r'C:\Users\LSC\.cache\codex-runtimes\codex-primary-runtime\dependencies')
renderer=Path(r'C:\Users\LSC\.codex\plugins\cache\openai-primary-runtime\documents\26.1015.12040\skills\documents\render_docx.py')
spec=importlib.util.spec_from_file_location('render_docx',renderer)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
os.environ['PATH']=str(DEPS/'native/poppler/Library/bin')+os.pathsep+os.environ['PATH']
pdf=QA/'word_render.pdf'
assert pdf.is_file() and pdf.stat().st_size>0
module.convert_to_pdf=lambda *args,**kwargs:(str(pdf),'Native Word PDF export')
pages=module.rasterize(str(ROOT/'报告/WAGO替换Weidmuller综合数据分析与评估_20261026.docx'),str(QA),130,False,False)
print('Rendered pages:',len(pages))
from pypdf import PdfReader
reader=PdfReader(pdf)
for i,page in enumerate(reader.pages,1):
    text=page.extract_text()
    (QA/f'page-{i}.txt').write_text(text,encoding='utf-8')
    print(i,len(text),text[:75].replace('\n',' '))
