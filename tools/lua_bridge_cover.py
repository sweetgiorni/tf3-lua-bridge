from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops
import math
import os
import sys
OUT=int(sys.argv[1]) if len(sys.argv)>1 else 1     # output scale: 1 = 1920x1080, 2 = 3840x2160, ...
BG_PATH=os.path.join(os.path.dirname(os.path.abspath(__file__)),'background.png')  # the official template
OUT_PATH=f'lua_bridge_cover_{OUT}x.png'
S=2*OUT; W,H=1920,1080
bg=Image.open(BG_PATH).convert('RGBA')
CY=(92,200,222); AM=(244,170,92); WH=(236,244,247,255); MUT=(150,174,186,255); DARK=(12,19,24,255); LIP=(52,128,148,255); LIGHT=(26,40,49,255)
P=lambda x,y:(x*S,y*S)
inter=lambda n,sz: ImageFont.truetype(f'/usr/share/fonts/opentype/inter/{n}.otf',sz*S)
def layer():
    im=Image.new('RGBA',(W*S,H*S),(0,0,0,0)); return im,ImageDraw.Draw(im)
def mask():
    m=Image.new('L',(W*S,H*S),0); return m,ImageDraw.Draw(m)
def circ(dr,cx,cy,r,**kw): dr.ellipse([(cx-r)*S,(cy-r)*S,(cx+r)*S,(cy+r)*S],**kw)

# ---------- text (upper right)
txt,td=layer()
def ltext(t,x,y,font,fill,sp=0):
    for c in t: td.text((x*S,y*S),c,font=font,fill=fill); x+=td.textlength(c,font=font)/S+sp
TX=1060
ltext("LUA",TX,30,inter('Inter-Bold',180),WH,8)
ltext("BRIDGE",TX,200,inter('Inter-Bold',180),WH,8)
td.rounded_rectangle([TX*S,413*S,(TX+110)*S,419*S],radius=3*S,fill=AM+(255,))
ltext("Transport Fever 3 mod, CLI, and MCP server",TX,435,inter("Inter-Regular",34),MUT,0.5)
ltext("for accessing the game's Lua API externally.",TX,479,inter("Inter-Regular",34),MUT,0.5)

# ---------- geometry (lower left)
ty=716; hy=ty-54
R=290; br=104; rim=16
pcx=430; dist=R-br-rim; ang=math.radians(50)
bx=pcx+dist*math.cos(ang); pcy=hy-dist*math.sin(ang)
mr=br; mx=1235; my=ty+12+mr
x_end=1400

full,fd=mask(); circ(fd,pcx,pcy,R,fill=255)
hole,hd=mask(); circ(hd,bx,hy,br,fill=255)
solid=ImageChops.subtract(full,hole)
ringm,rd=mask(); circ(rd,bx,hy,br,outline=255,width=9*S)
ringm=ImageChops.multiply(ringm,full)

inner,_=layer(); inner.paste(Image.new('RGBA',inner.size,DARK),(0,0),ImageChops.multiply(hole,full))
pl,_=layer(); pl.paste(Image.new('RGBA',pl.size,CY+(255,)),(0,0),solid); pl.paste(Image.new('RGBA',pl.size,LIP),(0,0),ringm)
mo,md=layer(); circ(md,mx,my,mr,fill=AM+(255,))

# ---------- track
world,wd=layer()
x0=bx-br+4; dl,gp=46,24; x=x0
while x+dl<=x_end:
    wd.rounded_rectangle([x*S,(ty-6)*S,(x+dl)*S,(ty+6)*S],radius=6*S,fill=AM+(255,)); x+=dl+gp

# ---------- steam train (local units, scaled about the train's rear-left at track level)
k=1.5; tx=bx-br-6
X=lambda x: tx+(x-tx)*k
Y=lambda y: ty+(y-ty)*k
def rr(x,y0,w,hh,r,fill): wd.rounded_rectangle([X(x)*S,Y(ty+y0)*S,X(x+w)*S,Y(ty+y0+hh)*S],radius=max(0.5,r*k)*S,fill=fill)
def poly(pts,fill): wd.polygon([P(X(a),Y(ty+b)) for a,b in pts],fill=fill)
WIN=(58,78,92,255)
def wheel(cx_,r):
    cy_=-2-r
    wd.ellipse([(X(cx_)-r*k)*S,(Y(ty+cy_)-r*k)*S,(X(cx_)+r*k)*S,(Y(ty+cy_)+r*k)*S],fill=LIGHT,outline=WH,width=int(2*S))
base=ty-14; h=44; cars=3; cw=96; gap=6; lw=120
x=tx
for i in range(cars):                       # carriages
    c0=tx+i*(cw+gap)
    rr(c0,-10-h,cw,h,8,WH)
    rr(c0+9,-10-h+10,cw-18,14,4,WIN)
    wheel(c0+16,6); wheel(c0+cw-16,6)
lx=tx+cars*(cw+gap)                         # lead car with sloped nose
poly([(lx,-10-h),(lx+lw-34,-10-h),(lx+lw,-10-h+22),(lx+lw,-10),(lx,-10)],WH)
poly([(lx+9,-10-h+10),(lx+lw-36,-10-h+10),(lx+lw-14,-10-h+24),(lx+9,-10-h+24)],WIN)
wheel(lx+22,6); wheel(lx+lw-26,6)
# dark outline, then hide everything behind the planet / tunnel rim
ol=world.getchannel('A').filter(ImageFilter.MaxFilter(5*S+1))
outline=Image.new('RGBA',world.size,DARK); outline.putalpha(ol)
world=Image.alpha_composite(outline,world)
left,ld=mask(); ld.rectangle([0,0,int(bx*S),H*S],fill=255)
occl=ImageChops.multiply(ImageChops.lighter(solid,ringm),left)
world.putalpha(ImageChops.multiply(world.getchannel('A'),ImageChops.invert(occl)))

sm,_=layer()

ov=txt
for l in (inner,pl,mo): ov=Image.alpha_composite(ov,l)
glow=ov.filter(ImageFilter.GaussianBlur(10*S))
out=Image.alpha_composite(bg.resize((W*S,H*S),Image.LANCZOS),glow)
out=Image.alpha_composite(out,ov)
for l in (world,sm): out=Image.alpha_composite(out,l)
out=out.resize((W*OUT,H*OUT),Image.LANCZOS)
out.convert('RGB').save(OUT_PATH)
