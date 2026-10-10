"""Usage: python3 tools/dump_ar.py <category_id>  -> index | question | answer | ar | qar"""
import json,sys,os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)),'..'))
d=json.load(open(f'q/{sys.argv[1]}.json'))
for i,q in enumerate(d['questions']): print(i,'|',q['q'],'|',q['a'],'|',q.get('ar',''),'|',q.get('qar',''))
