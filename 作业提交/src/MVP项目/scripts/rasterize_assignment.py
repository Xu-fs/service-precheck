"""对Word只读导出的PDF逐页栅格化，生成排版检查索引，不写入提交目录。"""
from pathlib import Path
import json
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / '.venv/assignment-qa/render'
index = []
for item in json.loads((QA / 'render-index.json').read_text(encoding='utf-8-sig')):
    pdf = pdfium.PdfDocument(item['pdf'])
    folder = Path(item['pdf']).parent
    for n in range(len(pdf)):
        page = pdf[n]
        image = page.render(scale=1.5).to_pil().convert('RGB')
        path = folder / f'page-{n+1}.png'
        image.save(path)
        index.append({'doc': Path(item['source']).name, 'page': n+1, 'image': str(path)})
    print(Path(item['source']).name, len(pdf))
    pdf.close()
(QA / 'page-index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding='utf-8')
font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 17)
for offset in range(0, len(index), 6):
    canvas = Image.new('RGB', (1800, 1780), '#d5dbe1')
    d = ImageDraw.Draw(canvas)
    for pos, item in enumerate(index[offset:offset+6]):
        x, y = (pos % 3)*600, (pos//3)*890
        im = Image.open(item['image'])
        im.thumbnail((570, 820))
        canvas.paste(im, (x+15, y+40))
        d.text((x+15, y+12), str(offset+pos+1)+' '+item['doc'].replace('.docx','')[:22]+' P'+str(item['page']), font=font, fill='black')
    canvas.save(QA / f'contact-{offset//6+1:02}.jpg', quality=90)
print('TOTAL PAGES', len(index))
