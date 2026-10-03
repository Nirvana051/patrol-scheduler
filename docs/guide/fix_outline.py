"""清理 Chromium 生成的书签：分页处的标题会被重复一遍（「11认识…认识…」），编号与标题之间也没空格。"""
import os
import re
import sys

from pypdf import PdfReader, PdfWriter      # pip install pypdf

src = sys.argv[1]
r = PdfReader(src)


def clean(t: str) -> str:
    t = t.strip()
    deduped = False
    for k in range(len(t) // 2, 0, -1):           # 去掉「前缀 + X + X」里重复的 X（取最长的 X）
        a = t[-k:]
        if t.endswith(a + a):
            t, deduped = t[:-k], True
            break
    if deduped:                                    # 文字重复过的，编号也重复了：「11认识」→ 1、「1212常见」→ 12
        m = re.match(r'^(\d+?|[A-C])\1(.*)$', t)
    else:                                          # 没重复的只补空格：「10更新」→ 10；「2.1 电脑要求」不动
        m = re.match(r'^(\d+|[A-C])(?=[^\d.\s])(.*)$', t)
    if not m:
        return t
    num, rest = m.group(1), m.group(2).strip()
    return rest if rest.startswith('附录') else f'{num}  {rest}'


def collect(items):
    out = []
    for it in items:
        if isinstance(it, list):
            out[-1][2].extend(collect(it))
        else:
            out.append([clean(it.title), r.get_destination_page_number(it), []])
    return out


tree = collect(r.outline)
w = PdfWriter()
w.append(r, import_outline=False)


def add(nodes, parent=None):
    for title, page, kids in nodes:
        ref = w.add_outline_item(title, page, parent=parent)
        add(kids, ref)


add(tree)
w.page_mode = '/UseOutlines'
w.add_metadata({'/Title': '巡检调度系统 桌面版 安装与配置指南', '/Subject': 'PatrolScheduler 0.8.0 Windows 安装、配置与使用',
                '/Author': 'PatrolScheduler', '/Creator': 'Chromium printToPDF + pypdf'})
tmp = src + '.tmp'
with open(tmp, 'wb') as f:
    w.write(f)
os.replace(tmp, src)


def show(nodes, d=0):
    for title, page, kids in nodes:
        print('  ' * d + f'- {title}  (p{page + 1})')
        show(kids, d + 1)


show(tree)
