"""Measure source lettering and fit its translation with local licensed fonts."""
from functools import lru_cache
from pathlib import Path
import math
import re
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

VERSION = 2
from backend.runtime import resource_root,custom_fonts_root
FONT_ROOT = resource_root() / 'assets/fonts'
FONTS = {
    'comic': FONT_ROOT/'ComicNeue-Bold.ttf',
    'comic-regular': FONT_ROOT/'ComicNeue-Regular.ttf',
    'hand': FONT_ROOT/'PatrickHand-Regular.ttf',
    'narration': FONT_ROOT/'YanoneKaffeesatz[wght].ttf',
    'title': FONT_ROOT/'Bangers-Regular.ttf',
    'comic-sans': Path('C:/Windows/Fonts/comicbd.ttf'),
    'impact': Path('C:/Windows/Fonts/impact.ttf'),
    'condensed': Path('C:/Windows/Fonts/bahnschrift.ttf'),
    'serif': Path('C:/Windows/Fonts/timesbd.ttf'),
    'arial': Path('C:/Windows/Fonts/arial.ttf'),
    'cjk-zh': Path('C:/Windows/Fonts/msyhbd.ttc'),
    'cjk-ja': Path('C:/Windows/Fonts/YuGothB.ttc'),
    'cjk-ko': Path('C:/Windows/Fonts/malgunbd.ttf'),
}


def available_fonts():
    # Users can supply the original licensed font without replacing application files.
    custom=custom_fonts_root()
    if custom.is_dir():
        for path in sorted(custom.iterdir()):
            if path.is_file() and path.suffix.lower() in {'.ttf','.otf'}:
                FONTS['custom-'+path.stem]=path
    return {key:str(path) for key,path in FONTS.items() if path.exists()}


@lru_cache(maxsize=512)
def font_for(name, size):
    path = FONTS.get(name, FONTS['comic'])
    if not path.exists():
        path = next((p for p in FONTS.values() if p.exists()), None)
    font = ImageFont.truetype(str(path), size) if path else ImageFont.load_default(size=size)
    if name == 'narration' and hasattr(font,'set_variation_by_axes'):
        font.set_variation_by_axes([500])
    return font


def text_mask(text, font, hollow=False):
    stroke = max(1, round(font.size*.035)) if hollow else 0
    box = font.getbbox(text, stroke_width=stroke)
    im = Image.new('L', (max(1,box[2]-box[0]+4), max(1,box[3]-box[1]+4)))
    ImageDraw.Draw(im).text((2-box[0],2-box[1]),text,font=font,fill=0 if hollow else 255,
                           stroke_width=stroke,stroke_fill=255)
    bounds=im.getbbox()
    return im.crop(bounds) if bounds else im


def line_ink(image, box):
    x1,y1,x2,y2=map(int,box)
    rgb=np.asarray(image.crop((x1,y1,x2,y2)).convert('RGB'))
    if not rgb.size:
        return np.zeros((1,1),np.uint8), (0,0,0), (255,255,255)
    gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    background=np.median(np.concatenate([rgb[0],rgb[-1],rgb[:,0],rgb[:,-1]]),axis=0)
    bright=np.mean(background)>145
    mask=gray < min(190,np.mean(background)-35) if bright else gray > max(155,np.mean(background)+65)
    pixels=rgb[mask]
    color=tuple(int(c) for c in np.median(pixels,axis=0)) if len(pixels) else ((0,0,0) if bright else (255,255,255))
    return mask.astype(np.uint8)*255,color,tuple(int(c) for c in background)


def analyze_style(image, region):
    available_fonts()
    lines=region.get('lines') or [{'box':region['box'],'text':region.get('source_text','')}]
    source=region.get('source_text','')
    letters=[c for c in source if c.isalpha()]
    style={'version':VERSION,'font':'comic','uppercase':bool(letters) and sum(c.isupper() for c in letters)/len(letters)>.85,
           'fill':[0,0,0],'background':[255,255,255],'hollow':False,'glow':None,'stretch':1.,
           'ink_height':float(np.median([line['box'][3]-line['box'][1] for line in lines])),
           'line_count':len(lines),'line_colors':[]}
    for line in lines:
        _,color,_=line_ink(image,line['box'])
        style['line_colors'].append(list(color))
    sample=max(lines,key=lambda line:len(line.get('text','')))
    text=sample.get('text',source)
    ink,color,bg=line_ink(image,sample['box'])
    style.update(fill=list(color),background=list(bg))
    bounds=cv2.findNonZero(ink)
    if bounds is None or not text.strip():
        return style
    x,y,w,h=cv2.boundingRect(bounds); observed=ink[y:y+h,x:x+w]
    padded=cv2.copyMakeBorder(observed,1,1,1,1,cv2.BORDER_CONSTANT,value=0)
    flooded=padded.copy();cv2.floodFill(flooded,None,(0,0),255)
    filled=padded|cv2.bitwise_not(flooded)
    hole_ratio=1.-np.count_nonzero(padded)/max(1,np.count_nonzero(filled))
    hollow_source=np.mean(bg)<145 and hole_ratio>.38
    shape=(min(900,w),max(8,min(80,h)))
    observed=cv2.resize(observed,shape,interpolation=cv2.INTER_AREA)>110
    best=None
    for name,path in FONTS.items():
        if not path.exists():continue
        if name.startswith('cjk-') and not re.search(r'[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]',text):continue
        if hollow_source and name not in {'impact','title','serif','condensed'} and not name.startswith('custom-'):continue
        for hollow in ([True] if hollow_source else [False]):
            candidate=text_mask(text,font_for(name,64),hollow)
            binary=cv2.resize(np.asarray(candidate),shape,interpolation=cv2.INTER_AREA)>110
            union=np.count_nonzero(observed|binary)
            overlap=np.count_nonzero(observed&binary)/max(1,union)
            ratio=(w/h)/(candidate.width/candidate.height)
            score=overlap-.12*abs(math.log(max(.1,ratio)))-.7*abs(float(observed.mean()-binary.mean()))
            if best is None or score>best[0]:best=(score,name,hollow,ratio)
    if best:
        style.update(font=best[1],hollow=best[2],stretch=round(float(np.clip(best[3],.72,1.18)),3),match_score=round(best[0],3))
    if np.mean(bg)<145:
        box=sample['box'];a,b,c,d=map(int,box)
        rgb=np.asarray(image.crop((max(0,a-6),max(0,b-6),min(image.width,c+6),min(image.height,d+6))))
        gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
        chroma=rgb.max(axis=2).astype(float)-rgb.min(axis=2)
        white_ink=(gray>185).astype(np.uint8)
        near_ink=cv2.dilate(white_ink,np.ones((11,11),np.uint8))>0
        halo=near_ink&(chroma>40)&(gray>max(30,np.mean(bg)+12))&(gray<215)
        if halo.sum()>ink.size*.005:
            style['glow']=[int(v) for v in np.median(rgb[halo],axis=0)]
    return style


def balanced_lines(text,font,width):
    cjk=bool(re.search(r'[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]',text))
    words=list(text) if cjk else text.split()
    separator='' if cjk else ' '
    if not words:return []
    # Dynamic programming prevents one-word tails and balances the whole block.
    n=len(words); costs=[float('inf')]*(n+1); costs[n]=0; next_word={}
    target=min(width,max(font.getlength(text)/max(1,math.ceil(font.getlength(text)/width)),width*.72))
    for i in range(n-1,-1,-1):
        for j in range(i+1,n+1):
            length=font.getlength(separator.join(words[i:j]))
            if length>width:break
            penalty=(target-length)**2
            if j==n and j-i==1 and n>2:penalty+=width**2*.35
            if penalty+costs[j]<costs[i]:costs[i]=penalty+costs[j];next_word[i]=j
    if 0 not in next_word:return None
    lines=[];i=0
    while i<n:
        j=next_word[i];lines.append(separator.join(words[i:j]));i=j
    return lines


def render(image,regions):
    output=image.convert('RGBA');warnings=[]
    for region in regions:
        text=region.get('target_text','').strip()
        if not text or region.get('status')=='ignored' or region.get('preserve_original'):continue
        style=dict(region.get('auto_style') or {'font':'comic','uppercase':region.get('source_text','').isupper(),
                                               'fill':[0,0,0],'line_count':2,'ink_height':32})
        style.update(region.get('style') or {})
        casing=style.get('casing','source')
        if casing=='upper' or (casing=='source' and style.get('uppercase')):text=text.upper()
        x1,y1,x2,y2=map(int,region['box']);width=max(1,x2-x1);height=max(1,y2-y1)
        stretch=float(np.clip(style.get('stretch',1),.65,1.3))
        preferred=style.get('font','comic');ink_height=style.get('ink_height',height/max(1,style.get('line_count',2)))
        if not region.get('style',{}).get('font'):
            for pattern,name in [(r'[\u3040-\u30ff]','cjk-ja'),(r'[\uac00-\ud7af]','cjk-ko'),(r'[\u3400-\u9fff]','cjk-zh'),(r'[\u0400-\u04ff]','arial')]:
                if re.search(pattern,text):
                    if FONTS[name].is_file():preferred=name
                    else:warnings.append(f"Police {name} absente : installez une police de cette langue pour la zone {region['id']}.")
                    break
        chosen=None
        for size in range(min(180,max(12,round(ink_height*1.7))),7,-1):
            font=font_for(preferred,size)
            lines=balanced_lines(text,font,(width-6)/stretch)
            if not lines:continue
            masks=[text_mask(line,font,style.get('hollow',False)) for line in lines]
            gap=max(2,round(ink_height*.22))
            total=sum(m.height for m in masks)+gap*(len(masks)-1)+4
            if total<=height and max(m.width for m in masks)*stretch<=width-2:
                chosen=(masks,gap,total,size);break
        if chosen is None:
            warnings.append(f"Texte trop long dans la zone {region['id']} ; réduire ou agrandir la zone.")
            continue
        masks,gap,total,size=chosen
        layer=Image.new('RGBA',(width,height));yy=(height-total)//2+2
        for i,mask in enumerate(masks):
            mask=mask.resize((max(1,round(mask.width*stretch)),mask.height),Image.Resampling.LANCZOS)
            xx=(width-mask.width)//2
            colors=style.get('line_colors') or [style.get('fill',[0,0,0])]
            color=style.get('fill',[0,0,0]) if region.get('style',{}).get('fill') else colors[min(len(colors)-1,round(i*(len(colors)-1)/max(1,len(masks)-1)))]
            if style.get('glow'):
                glowmask=Image.new('L',(width,height));glowmask.paste(mask,(xx,yy))
                glowmask=glowmask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(max(1,size*.045)))
                glow=Image.new('RGBA',layer.size,tuple(style['glow'])+(0,));glow.putalpha(glowmask)
                layer=Image.alpha_composite(layer,glow)
            ink=Image.new('RGBA',mask.size,tuple(color)+(0,));ink.putalpha(mask)
            layer.alpha_composite(ink,(xx,yy));yy+=mask.height+gap
        output.alpha_composite(layer,(x1,y1))
    return output.convert('RGB'),warnings
