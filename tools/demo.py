"""Read-only UI demo with fictional data. Standard library only; no external calls."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

STATIC=Path(__file__).resolve().parents[1]/'analysis/static'
TITLES=['制造企业知识库：先让一线员工找得到答案','项目申报资料：把零散材料整理成审核清单',
        '客户咨询助手：自动整理需求，保留人工决策','培训内容复用：从长录音到一篇能讲清的案例',
        '售后工单归类：先统一分类，再谈自动派单','产品试点交付：用小范围验证换来下一步']
TEXT='''这次我想讲一个我们设计的制造企业知识库试点。客户的问题不是资料少，而是资料散在不同文件夹和聊天记录里。一线同事每次遇到问题，都要反复问同样的人。

我们先没有承诺搭一个包打天下的系统，而是把试点缩小到一个部门、几类常见问题。第一步，请业务负责人列出最常被问到的问题，并确认哪些资料可以作为答案依据。

第二步，把资料按设备、流程和责任人整理好。助手给出答案时，也提供出处；没有明确依据的内容转给人工确认。第三步，让少量同事试用，记录答不上的问题，再补充资料和调整入口。

这个示例的商业逻辑，是先验证能否减少重复沟通，再决定是否扩大投入。实际节省时间、成交金额和最终交付效果，需要真实试点数据支持，不能预先编造。

【演示数据】以上是虚构的方案示例，用于展示页面功能，不代表真实客户项目或已取得的成果。'''

PROFILES = [
    ('资料分散、重复问答多', '整理资料和问题清单 → 小范围试用 → 答案附来源 → 无依据时交给人工'),
    ('申报材料分散、缺项难以发现', '确认申报要求 → 对照材料清单 → 标出缺项和出处 → 交由负责人审核'),
    ('咨询记录零散、需求交接不完整', '采集已授权的咨询内容 → 整理需求 → 标注不确定信息 → 人工确认后分配跟进'),
    ('培训录音很长、案例难以重复利用', '转写录音 → 汇集同一项目的证据 → 整理案例与总结 → 讲师核实后使用'),
    ('售后分类不统一、工单流转反复', '梳理历史工单 → 统一分类 → 抽样核验 → 低置信度交给人工 → 小范围验证派单'),
    ('产品投入前缺少可验证的试点边界', '选择一个场景 → 约定范围和验收标准 → 小范围交付 → 收集反馈 → 决定是否扩大'),
]


def demo_content(index):
    need, delivery = PROFILES[index]
    text = TEXT if index == 0 else (
        f'这次我想讲的是“{TITLES[index]}”这个方案示例。客户面对的问题是{need}。'
        f'我们先确认使用者、业务负责人和最小试点范围，避免一开始就承诺全面自动化。\n\n'
        f'交付按照这个顺序推进：{delivery}。每一步都保留可核对的依据，'
        '遇到材料不足或者判断不确定的地方，由业务负责人确认。\n\n'
        '验收先看流程能否跑通、输出能否被业务人员使用，再通过试点记录评估收益。'
        '是否扩大投入，由实际反馈决定；没有验证的数据不写成既得成绩。\n\n'
        '【演示数据】这是虚构方案，不代表真实客户、成交或已取得的效果。'
    )
    return text, need, delivery


def rows():
    return [dict(id=f'demo-{i}',job_id=f'demo-job-{i}',case_index=0,title=title,stage='演示方案',
                 created=1793577600+i,updated=1793577600+i,locked=False,summary_available=True,
                 excerpt='围绕客户需求、产品选择、交付步骤和商业价值，讲清一个完整项目。',version='demo')
            for i,title in enumerate(TITLES)]


def api(key):
    if key=='auth':return {'csrf':'demo-read-only'}
    if key in {'recordings','recordings/transcribed','jobs','projects'}:return []
    if key=='feed':return {'entries':[],'automation':{'enabled':True,'pending':0,'transcribing':0,
        'daily':{'enabled':True,'processing_time':'09:00','send_time':'11:00','webhook_configured':False,'delivery_enabled':False}}}
    if key=='status':return {'models_ready':False,'filtering':{},'model':'演示模式','usage':{'committed':0},'daily_budget':1000000,'task_budget':200000}
    if key=='cases':return rows()
    if key.startswith('cases/'):
        row=next((r for r in rows() if r['id']==key[6:]),None)
        if row:
            text, need, delivery = demo_content(int(row['id'].split('-')[1]))
            return dict(row,case={'title':row['title'],'stage':'演示方案','text':text,'refs':[],
                                  'missing_details':['真实试点结果与量化收益']},script={'evidence':[]},summary_scope='case',
                        summary={'items':[
                            {'heading':'客户需求','text':need+'。先选择范围明确的高频场景，确认使用者与负责人。','refs':[]},
                            {'heading':'产品与交付','text':delivery+' → 根据反馈完善。','refs':[]},
                            {'heading':'商业逻辑','text':'把试点边界和验收标准讲清楚，用实际使用反馈决定是否扩大投入。','refs':[]},
                            {'heading':'结果与待核实','text':'本页面全部为虚构演示数据；没有声称已成交、已节约费用或已取得真实业务成果。','refs':[]}
                        ],'evidence':[]})
    return None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=urlsplit(self.path).path
        if path.startswith('/api/'):
            data=api(path[5:])
            return self.respond(200 if data is not None else 404,json.dumps(data,ensure_ascii=False).encode(),'application/json; charset=utf-8')
        name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}.get(path)
        if not name:return self.respond(404,b'Not found','text/plain')
        data=(STATIC/name).read_bytes()
        if name=='index.html':
            notice='<div style="padding:12px;text-align:center;background:#183c62;color:#d8efff;font:14px sans-serif">演示模式 · 虚构示例数据 · 不录音、不调用模型、不发送群消息</div>'
            data=data.replace(b'<body>',('<body>'+notice).encode())
        self.respond(200,data,{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[name.split('.')[-1]])

    def do_POST(self):self.respond(405,b'Read-only demo','text/plain')

    def respond(self,status,data,kind):
        self.send_response(status);self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
        self.end_headers();self.wfile.write(data)

    def log_message(self,*args):pass


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8788)
    args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'Fictional read-only demo: http://127.0.0.1:{args.port}/',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
