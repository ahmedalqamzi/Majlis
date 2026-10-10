"""Usage: python3 tools/dump.py <category_id>  -> prints index | points | question | answer"""
import json,sys,os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)),'..'))
d=json.load(open(f'q/{sys.argv[1]}.json'))
print(d['name'])
for i,q in enumerate(d['questions']): print(i,'|',q['p'],'|',q['q'],'|',q['a'])
