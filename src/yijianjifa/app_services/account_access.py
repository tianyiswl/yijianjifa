"""Account metadata only; platform sessions never cross into Web UI."""
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog


def public_account(account):
    state=account.login_state
    if state=='valid' and not account.session_ref:
        state='pending_verification'
    return {**{k:getattr(account,k) for k in ('id','platform','nickname','remark','group_name','is_default','avatar_data')},
            'login_state':state,
            'last_login_at':account.last_login_at.isoformat() if account.last_login_at else None,
            'capabilities':{k:(getattr(account.capabilities,k) if getattr(account.capabilities,k) in ({'pending','local','national','local_national'} if k=='location_scope' else {'available','not_found','pending'}) else 'pending') for k in ('ordinary','commerce','location_scope')}}


class AccountAccess:
    def __init__(self,engine):
        self.core=AccountService(engine)
        self.catalog=PlatformCatalog()

    def get(self,pid):
        account=self.core.get(pid)
        self.catalog.get(account.platform)
        return public_account(account)

    def list(self,query,platform,group,state,offset,limit):
        if platform:self.catalog.get(platform)
        supported={p['platform'] for p in self.catalog.list()}
        # Group choices belong to the whole account library, not the current filter.
        all_rows=[public_account(a) for a in self.core.list() if a.platform in supported]
        states={'ok':'valid','relogin':'expired','pending':'pending_verification'}
        rows=[r for r in all_rows if (not platform or r['platform']==platform)
              and (not group or r['group_name']==group)
              and (not state or r['login_state']==states[state])
              and query.casefold() in ' '.join([r['id'],r['nickname'],r['remark']]).casefold()]
        return {'total':len(rows),'items':rows[offset:offset+limit],
                'groups':sorted({r['group_name'] for r in all_rows if r['group_name']})}
