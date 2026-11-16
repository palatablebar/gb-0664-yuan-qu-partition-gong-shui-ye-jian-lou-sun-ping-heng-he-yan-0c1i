from fastapi import APIRouter,Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from app import catalog as registry
from app.config import TITLE,MODULES,RULE_VERSION
from app.modules.m7_batches.service import run
from app.modules.m8_adjust import service as adjustments
from app.errors import DomainError
from app.store import fetch
router=APIRouter();templates=Jinja2Templates(directory="app/templates")
@router.get("/")
def home():return RedirectResponse("/calculate")

def _adjust_page(request,run_id,adjustment_id):
    data=fetch(run_id) if run_id else run({"sample":"dirty"})
    active,versions=registry.versions()
    items=adjustments.listing(data["manifest"]["run_id"])
    chosen=None;problem=None
    if adjustment_id:
        try:chosen=adjustments.load(adjustment_id)
        except DomainError as exc:problem=str(exc)
    elif items:chosen=adjustments.load(items[0]["id"])
    return templates.TemplateResponse(request=request,name="adjust.html",context={"data":data,"title":TITLE,"modules":MODULES,"slug":"adjust","page_title":"调整重算","version":RULE_VERSION,"catalogs":versions,"active_catalog":active,"adjustments":items,"adjustment":chosen,"problem":problem})

def page(request:Request,slug:str,run_id:str|None=None):
    if slug=="adjust":return _adjust_page(request,run_id,None)
    data=fetch(run_id) if run_id else run({"sample":"dirty"})
    return templates.TemplateResponse(request=request,name=slug+".html",context={"data":data,"title":TITLE,"modules":MODULES,"slug":slug,"page_title":dict(MODULES)[slug],"version":RULE_VERSION})
for slug,label in MODULES:
    if slug=="adjust":
        def adjust_view(request:Request,run_id:str|None=None,adjustment:str|None=None):return _adjust_page(request,run_id,adjustment)
        router.add_api_route("/adjust",adjust_view,methods=["GET"],include_in_schema=False)
        continue
    def view(request:Request,run_id:str|None=None,_slug=slug):return page(request,_slug,run_id)
    router.add_api_route("/"+slug,view,methods=["GET"],include_in_schema=False)
