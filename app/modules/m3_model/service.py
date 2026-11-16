import json
from app.config import BASE_CATALOG_VERSION, SAMPLE_DIR
from app.modules.m2_contract.service import run as previous
from .rules import validate_catalog,match
def run(options=None):
    result=previous(options);catalog=json.loads((SAMPLE_DIR/"catalog.json").read_text())
    result["zones"]=validate_catalog(catalog);valid=[]
    for row in result["rows"]:
        if match(row,catalog):valid.append(row)
        else:result["quarantine"].append({"line":row["line"],"code":"REFERENCE","raw":row.get("source",row)})
    result["rows"]=valid;result["catalog"]=catalog
    # 原账固定使用建档时的目录版本；目录改版只影响调整批次，不改写历史账。
    result["catalog_version"]=BASE_CATALOG_VERSION
    return result
