from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .model import filename, shard, valid_command

class Dataset:
    def __init__(self, root: str | Path):
        self.root = Path(root); self.data = self.root / "data"
    @property
    def metadata(self): return json.loads((self.data / "metadata.json").read_text())
    def get(self, name: str):
        if not valid_command(name): return None
        path = self.data / "executables" / shard(name) / filename(name)
        if not path.exists(): return None
        value = json.loads(path.read_text()); return value if value["command"] == name else None
    def check(self, name: str) -> dict[str, Any]:
        meta = self.metadata; record = self.get(name)
        complete = bool(meta.get("checked_sources")) and all(v.get("status") == "success" for v in meta.get("coverage", {}).values())
        status = "collision" if record else ("clear_in_index" if complete else "unknown")
        result = {"name": name, "status": status, "snapshot": meta["snapshot"], "checked_sources": meta.get("checked_sources", [])}
        if record: result["providers"] = record["providers"]
        return result
    def search(self, prefix="", length=None, ecosystem=None, limit=100):
        names = []
        for path in sorted((self.data / "executables").glob("**/*.json")):
            r=json.loads(path.read_text()); n=r["command"]
            if prefix and not n.startswith(prefix): continue
            if length is not None and len(n) != length: continue
            if ecosystem and ecosystem not in {p["ecosystem"] for p in r["providers"]}: continue
            names.append(n)
        return names[:limit]
    def similar(self, name: str, limit=20):
        def distance(a,b):
            row=list(range(len(b)+1))
            for i,x in enumerate(a,1):
                nxt=[i]+[0]*len(b)
                for j,y in enumerate(b,1): nxt[j]=min(nxt[j-1]+1,row[j]+1,row[j-1]+(x!=y))
                row=nxt
            return row[-1]
        def grams(s): s=f"  {s.casefold()}  "; return {s[i:i+3] for i in range(len(s)-2)}
        target=grams(name); found=[]
        for candidate in self.search(limit=1_000_000):
            cg=grams(candidate); similarity=len(target&cg)/len(target|cg)
            d=distance(name.casefold(),candidate.casefold())
            if candidate.startswith(name) or name.startswith(candidate) or d <= 2 or similarity >= .3:
                found.append({"name":candidate,"edit_distance":d,"trigram_similarity":round(similarity,3)})
        return sorted(found,key=lambda x:(x["edit_distance"],-x["trigram_similarity"],x["name"]))[:limit]
