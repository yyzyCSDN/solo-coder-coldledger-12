from datetime import timedelta
from .inventory.fefo import reserve
from .quality.release import may_release
from .transport.connections import choose
from .dispatch.express_allocation import (
    allocate as _express_allocate,
    replan as _express_replan,
    screen as _express_screen,
    require_complete as _express_require,
    explain as _express_explain,
)
class ColdLedgerService:
    def reserve_inventory(self,*args,**kwargs): return reserve(*args,**kwargs)
    def release_decision(self,*args,**kwargs): return may_release(*args,**kwargs)
    def next_connection(self,inbound,candidates,minutes): return choose(inbound,candidates,timedelta(minutes=minutes))
    def health(self): return {'service':'coldledger','status':'ok','runtime_dependencies':0}
    def allocate_express(self,tasks,options,reservations=None,exclude=frozenset()):
        return _express_allocate(tasks,options,reservations,exclude)
    def replan_express(self,plan,tasks,options,failed_options):
        return _express_replan(plan,tasks,options,failed_options)
    def screen_express(self,task,options,reservations=None,exclude=frozenset()):
        return _express_screen(task,options,reservations,exclude)
    def require_complete_express(self,plan,tasks):
        return _express_require(plan,tasks)
    def explain_express(self,plan):
        return _express_explain(plan)
