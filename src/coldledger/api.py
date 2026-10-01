from datetime import timedelta
from .inventory.fefo import reserve
from .quality.release import may_release
from .transport.connections import choose
from .dispatch.urgent_assignment import choose as choose_urgent, assign as assign_urgent, replan as replan_urgent
class ColdLedgerService:
    def reserve_inventory(self,*args,**kwargs): return reserve(*args,**kwargs)
    def release_decision(self,*args,**kwargs): return may_release(*args,**kwargs)
    def next_connection(self,inbound,candidates,minutes): return choose(inbound,candidates,timedelta(minutes=minutes))
    def choose_urgent_carrier(self,*args,**kwargs): return choose_urgent(*args,**kwargs)
    def assign_urgent(self,*args,**kwargs): return assign_urgent(*args,**kwargs)
    def replan_urgent(self,*args,**kwargs): return replan_urgent(*args,**kwargs)
    def health(self): return {'service':'coldledger','status':'ok','runtime_dependencies':0}
