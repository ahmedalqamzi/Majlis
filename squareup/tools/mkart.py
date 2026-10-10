"""Copy Fluent Emoji 3D art (MIT, @lobehub/fluent-emoji-3d) for each category and team avatar into squareup/art/."""
import os,shutil,sys,collections
SRC=sys.argv[1]  # .../package/assets
OUT=os.path.join(os.path.dirname(os.path.abspath(__file__)),'..','art')
CAT={
'uaehistory':'🏰','heritage':'☕','abudhabi':'🕌','dubai':'🏙️','sharjah':'📚','rak':'⛰️','alain':'🌴','eastcoast':'🏖️','ajmanuaq':'⛵',
'uaenature':'🦎','uaefastfood':'🍔','emiratidialect':'💬','emiratifood':'🍲','uaesymbols':'🦅','uaetv':'📺','uaespace':'🚀','uaesports':'🏇',
'ramadan':'🌙','uaebiz':'💼','uaetransport':'🚇','uaeislands':'🏝️','uaemalls':'🛍️',
'arabworld':'🌍','saudi':'🐪','oman':'🏜️','qatar':'🏟️','kuwait':'🗼','bahrain':'🦪','egypt':'🏺','levant':'🫒','maghreb':'🫖','arabic':'✍️',
'proverbs':'📜','islamicgolden':'🔭','islamichistory':'⏳','khaleejimusic':'🥁','arabicmusic':'🪕','khaleejitv':'🎭','egyptiancinema':'🎬',
'anime':'🌸','onepiece':'🏴‍☠️','jjk':'👹','naruto':'🍥','dragonball':'🐉','aot':'🧱','demonslayer':'⚔️','deathnote':'📓','hxh':'🎣','mha':'💥',
'bleach':'🗡️','ghibli':'🍃','fma':'⚗️','chainsawman':'🪚','sololeveling':'👑','conan':'🔍','spacetoon':'🪐','manga':'📖','tcg':'🃏','animeost':'🎶',
'videogames':'🎮','gaminghistory':'👾','tga':'🏆','warcraft':'🪓','pokemon':'⚡','minecraft':'⛏️','fortnite':'🪂','cod':'🎖️','gta':'🚓','eafc':'🥅',
'zelda':'🛡️','mario':'🍄','fromsoft':'💀','lol':'🧙','esports':'🖥️','playstation':'🕹️','xbox':'🎯','nintendo':'🎲','finalfantasy':'🔮','assassins':'🥷',
'residentevil':'🧟','witcher':'🐺','overwatch':'🤖','roblox':'🧸','mobilegames':'📱','fighting':'👊','retro':'📼','gameost':'🎧',
'movies':'🍿','oscars':'🌟','sitcoms':'🛋️','superheroes':'🦸','music':'🎵','rap90s':'🎤','mcu':'🦾','starwars':'🌌','harrypotter':'🪄','disney':'👸',
'lotr':'💍','got':'🐲','breakingbad':'🧪','pixar':'💡','simpsons':'🍩','cartoons':'🐭','kpop':'💜','kdrama':'💌','bollywood':'💃','horror':'👻',
'netflix':'🎞️','villains':'😈','hiphop00':'💿','pop00':'🎙️','rock':'🎸','screenthemes':'🎻',
'soccer':'⚽','worldcup':'🌐','f1':'🏎️','padel':'🏓','sports':'🏅','cars':'🚙','epl':'🦁','nba':'🏀','olympics':'🥇','ufc':'🥋','laliga':'🐂',
'ucl':'⭐','tennis':'🎾','boxing':'🥊','wwe':'🤼','cricket':'🏏','uaefootball':'👟',
'general':'🧠','geography':'🗺️','history':'🏛️','japan':'🗾','science':'🔬','math':'🧮','gcse':'🎓','food':'🍕','brands':'🏷️','riddles':'🧩',
'emoji':'😎','capitals':'🌆','space':'🌕','humanbody':'🫀','animals':'🐘','landmarks':'🗽','dinosaurs':'🦖','inventions':'⚙️','chemistry':'🧫',
'physics':'🧲','mythology':'🔱','flags':'🚩','logos':'🔖',
}
TEAM=['🦅','🐪','🐎','🦁','🐆','🐺','🦊','🐢','🐬','🦈','🐙','🦉','🐐','🦌','🐫','🦜']
HOME=['🐪','☕','🌴','🎮','⚽','🍿','🦅','🏆','🎤','🧠','🌙','🏎️']
def fname(e):
    cps=[f'{ord(c):x}' for c in e]
    for cand in ('-'.join(cps),'-'.join(c for c in cps if c!='fe0f')):
        p=os.path.join(SRC,cand+'.webp')
        if os.path.exists(p): return cand,p
    return None,None
dup=[e for e,n in collections.Counter(CAT.values()).items() if n>1]
if dup: print('DUPLICATES',dup)
os.makedirs(OUT,exist_ok=True);miss=[]
want={**{f'cat-{k}':v for k,v in CAT.items()},**{f'team-{i}':e for i,e in enumerate(TEAM)},**{f'home-{i}':e for i,e in enumerate(HOME)}}
for k,e in want.items():
    c,p=fname(e)
    if not p: miss.append((k,e));continue
    shutil.copy(p,os.path.join(OUT,k+'.webp'))
print('missing',miss);print(len(want)-len(miss),'copied')
