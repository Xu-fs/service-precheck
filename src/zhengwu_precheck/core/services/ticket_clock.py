"""工单示例日历：接手按工作日，答复按连续小时，关闭按上海自然日。"""
from datetime import date, datetime, time as daytime, timedelta, timezone
import json
import os
from pathlib import Path

# 首期仅处理当前及未来日期；上海使用UTC+8，不依赖Windows系统时区配置。
SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')


class WorkCalendar:
    def __init__(self, holidays=(), workdays=(), name='示例周一至周五（无节假日）'):
        self.holidays = frozenset(date.fromisoformat(v) for v in holidays)
        self.workdays = frozenset(date.fromisoformat(v) for v in workdays)
        if self.holidays & self.workdays:
            raise ValueError('同一天不能同时配置工作日与休息日')
        self.name = name

    @classmethod
    def configured(cls):
        # 客户日历位于仓库外；只包含非机密日期，不自动联网取法定日历。
        path = os.getenv('ZHENGWU_WORK_CALENDAR')
        if not path:
            return cls()
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return cls(data.get('holidays', []), data.get('workdays', []), data.get('name', '配置工作日历'))

    def accept_due(self, created):
        local = datetime.fromtimestamp(created, SHANGHAI)
        # 示例口径：下一工作日的同一时刻；特殊调休日期优先于周末。
        for _ in range(3700):
            local += timedelta(days=1)
            d = local.date()
            if d in self.workdays or (d.weekday() < 5 and d not in self.holidays):
                return local.timestamp()
        raise ValueError('日历没有可用工作日')

    def snapshot(self):
        return json.dumps({'name':self.name,'timezone':'Asia/Shanghai','method':'下一工作日同一时刻',
            'holidays':sorted(map(str,self.holidays)),'workdays':sorted(map(str,self.workdays))},ensure_ascii=False)


def waiting_deadlines(now):
    local = datetime.fromtimestamp(now, SHANGHAI)
    # 第14天提醒后保留至当天结束：以第15天00:00作为排他关闭边界。
    return {'reminder7':(local+timedelta(days=7)).timestamp(),
            'reminder14':(local+timedelta(days=14)).timestamp(),
            'close':datetime.combine(local.date()+timedelta(days=15),daytime.min,SHANGHAI).timestamp()}
