"""把 export/maas_js 下的 bundle 解析成"路由 → chunk → 接口调用"清单。

bundles.py 只捞出裸路径字符串，很多接口是 `"/api/Container/"+id+"/"+key` 拼出来的，
光看字符串会漏掉后半截。这里直接找 `$api.get(...)` 这类调用点，把整个第一参数表达式
抠出来还原成 `/api/Container/{id}/{key}` 这样的模板。

    python -m maas.endpoints        # 产出 export/maas_api_map.json 并打印摘要
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from maas.recon import OUT

JS_DIR = OUT.parent / "maas_js"
MAP = OUT.parent / "maas_api_map.json"

# app.js 里的路由表：{path:"/OutTruck",meta:{...},component:function(){return Promise.all([n.e(0),n.e(1),n.e(35)])...
ROUTE_RE = re.compile(r'\{path:"(/[^"]*)"(?:,meta:\{([^{}]*)\})?,component:function\(\)\{return[^}]{0,120}?((?:n\.e\(\d+\)[,\]]){1,6})')
CHUNK_RE = re.compile(r"n\.e\((\d+)\)")
CALL_RE = re.compile(r"\$api\.(get|post|put|delete|patch)\(")
# 拼接表达式里的字符串片段与变量片段
LIT_RE = re.compile(r'^"([^"]*)"$|^\'([^\']*)\'$')


def first_arg(text: str, start: int) -> str:
    """从 `(` 后开始按括号配平扫，取到第一个顶层逗号或右括号为止。"""
    depth, buf = 0, []
    for ch in text[start:start + 4000]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        buf.append(ch)
    return "".join(buf).strip()


CONCAT_RE = re.compile(r"\.concat\(([^()]*)\)")


def template(expr: str) -> str:
    """`"/api/X/"+i+"/"+a` → `/api/X/{i}/{a}`；纯字面量原样返回。

    babel 会把部分模板字符串编成 `"/api/X?a=".concat(t,"&b=").concat(e)`，
    先摊平成 `+` 拼接再走同一套逻辑，不然这些接口会整条丢掉。
    """
    while CONCAT_RE.search(expr):
        expr = CONCAT_RE.sub(lambda m: "+" + "+".join(a.strip() for a in m.group(1).split(",")), expr, count=1)
    parts = []
    for piece in re.split(r"\+(?=(?:[^\"']*[\"'][^\"']*[\"'])*[^\"']*$)", expr):
        piece = piece.strip()
        m = LIT_RE.match(piece)
        if m:
            parts.append(m.group(1) if m.group(1) is not None else m.group(2))
        elif piece:
            parts.append("{" + piece.replace("this.", "").replace("e.", "")[:40] + "}")
    return "".join(parts)


def calls_in(text: str) -> list[dict[str, str]]:
    out = []
    for m in CALL_RE.finditer(text):
        expr = first_arg(text, m.end())
        if not expr:
            continue
        out.append({"method": m.group(1).upper(), "path": template(expr), "expr": expr[:300]})
    return out


# Quasar 的 q-table 列定义：{name:"cntr_no1",label:"拖运箱一",align:"center",field:"cntr_no1"}
# 这是现成的"响应字段 → 中文名"字典，比自己猜字段含义靠谱。
COL_RE = re.compile(r'\{name:"([\w.]+)",label:"([^"]+)"[^}]*?field:"?([\w.]+)"?')


def columns_in(text: str) -> dict[str, str]:
    return {m.group(3): m.group(2) for m in COL_RE.finditer(text)}


def routes_of(app_js: str) -> dict[str, dict]:
    out = {}
    for m in ROUTE_RE.finditer(app_js):
        path, meta, chunks = m.group(1), m.group(2) or "", m.group(3)
        out[path] = {"meta": meta, "chunks": sorted({int(c) for c in CHUNK_RE.findall(chunks)})}
    return out


def main() -> None:
    files = {p.name: p.read_text("utf-8", "replace") for p in JS_DIR.glob("ghzh.*.js")}
    app = next((v for k, v in files.items() if "app." in k), "")
    routes = routes_of(app)

    # chunk id -> 文件名（manifest 里 1 号叫 chunk-common，其余就是数字前缀）
    by_chunk: dict[int, str] = {}
    for name in files:
        stem = name.split("__", 1)[-1].split(".", 1)[0]
        if stem.isdigit():
            by_chunk[int(stem)] = name
        elif stem == "chunk-common":
            by_chunk[1] = name

    chunk_calls = {cid: calls_in(files[n]) for cid, n in by_chunk.items()}
    chunk_cols = {cid: columns_in(files[n]) for cid, n in by_chunk.items()}
    # chunk 0/1 是公共块，几乎每个路由都带着它；把它们的字段算进某个页面会串味
    # （船期查询会莫名其妙冒出一堆箱货字段），所以只认这个路由独占的 chunk。
    used: dict[int, int] = {}
    for r in routes.values():
        for cid in r["chunks"]:
            used[cid] = used.get(cid, 0) + 1

    for r in routes.values():
        seen, merged = set(), []
        for cid in r["chunks"]:
            for c in chunk_calls.get(cid, []):
                if c["path"] not in seen:
                    seen.add(c["path"])
                    merged.append(c)
        r["api"] = merged
        r["fields"] = {k: v for cid in r["chunks"] if used[cid] == 1
                       for k, v in chunk_cols.get(cid, {}).items()}

    every = {}
    for cid, cs in chunk_calls.items():
        for c in cs:
            every.setdefault(f'{c["method"]} {c["path"]}', {**c, "chunks": []})["chunks"].append(cid)

    MAP.write_text(json.dumps({"routes": routes, "all": every}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(routes)} 个路由，{len(every)} 个不同接口 -> {MAP}\n")
    for k in sorted(every):
        print(" ", k)


if __name__ == "__main__":
    main()
