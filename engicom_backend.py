"""Persistencia SQLite y cálculos por línea. No almacena PDFs ni DataFrames completos en RAM."""
import io, os, re, sqlite3, zipfile, hashlib, tempfile, math, gc
from pathlib import Path
from collections import Counter
import pandas as pd
import numpy as np
from engicom_core import parse_pdf, COLS

FAMILIES=['Apoyos','Levas','chatter Levas','Chatter apoyos','Chatter Journal Ford']
SCOPES=['Acumulado','Línea 1','Línea 2','Sin identificar']


def connect(path):
    db=sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.execute('PRAGMA temp_store=FILE')
    db.execute('PRAGMA cache_size=-8000')
    return db

def initialize(path):
    db=connect(path)
    db.executescript('''CREATE TABLE IF NOT EXISTS pieces(id INTEGER PRIMARY KEY, filename TEXT, line TEXT, gage TEXT, digest TEXT UNIQUE, nrows INTEGER DEFAULT 0, error TEXT);
    CREATE TABLE IF NOT EXISTS measurements(piece INTEGER, filename TEXT, line TEXT, family TEXT, leva TEXT, apoyo TEXT, characteristic TEXT, measurement REAL, area TEXT, result TEXT, lsl REAL, usl REAL);
    CREATE INDEX IF NOT EXISTS ix_line_piece ON measurements(line,piece);
    CREATE INDEX IF NOT EXISTS ix_area_char ON measurements(line,area,characteristic);
    CREATE INDEX IF NOT EXISTS ix_result ON measurements(line,result);
    ''')
    db.commit();return db

def classify(text, gage_line1='8377', gage_line2='8375'):
    m=re.search(r'Gage\s*ID\s*:\s*(\d+)(?:\s*[-–—]\s*(?:Line|Línea|Linea)\s*[:#-]?\s*(\d+))?',text,re.I)
    if not m:return 'Sin identificar',''
    gage=m.group(1); explicit=m.group(2)
    if explicit:return 'Línea '+explicit if explicit in ('1','2') else 'Sin identificar',gage
    if gage==str(gage_line1):return 'Línea 1',gage
    if gage==str(gage_line2):return 'Línea 2',gage
    return 'Sin identificar',gage

def iter_pdfs(paths):
    """Lee un PDF cada vez desde archivos locales, sin cargar todo un ZIP en RAM."""
    for path in paths:
        path=Path(path)
        if path.suffix.lower()=='.pdf':
            with path.open('rb') as f:yield path.name,f.read(),None
        elif path.suffix.lower()=='.zip':
            with zipfile.ZipFile(path) as z:
                for member in z.infolist():
                    if not member.is_dir() and member.filename.lower().endswith('.pdf'):
                        try:
                            if member.file_size>40*1024*1024:raise ValueError('PDF individual supera 40 MB')
                            yield Path(member.filename).name,z.read(member),None
                        except Exception as exc:yield member.filename,None,str(exc)

def pdf_count(paths):
    total=0
    for path in paths:
        try:
            if str(path).lower().endswith('.pdf'):total+=1
            elif str(path).lower().endswith('.zip'):
                with zipfile.ZipFile(path) as z:total+=sum(1 for x in z.infolist() if x.filename.lower().endswith('.pdf') and not x.is_dir())
        except Exception:pass
    return total

def process(paths,db,gage_line1='8377',gage_line2='8375',progress=None):
    import pdfplumber
    total=pdf_count(paths); done=0; duplicates=0; failures=0
    for filename,blob,read_error in iter_pdfs(paths):
        done+=1
        if read_error or blob is None:
            db.execute('INSERT INTO pieces(filename,line,gage,error) VALUES(?,?,?,?)',(filename,'Sin identificar','',read_error));failures+=1
            continue
        digest=hashlib.sha256(blob).hexdigest()
        if db.execute('SELECT 1 FROM pieces WHERE digest=?',(digest,)).fetchone():
            duplicates+=1;continue
        try:
            with pdfplumber.open(io.BytesIO(blob)) as pdf:
                header=(pdf.pages[0].extract_text() or '') + '\n' + ((pdf.pages[1].extract_text() or '')[:600] if len(pdf.pages)>1 else '')
            line,gage=classify(header,gage_line1,gage_line2)
            cursor=db.execute('INSERT INTO pieces(filename,line,gage,digest) VALUES(?,?,?,?)',(filename,line,gage,digest))
            piece=cursor.lastrowid
            parsed,err=parse_pdf(filename,blob,piece)
            # Fuente única de límites: tablas de especificación del dibujo en engicom_core.
            # Nunca reemplazar criterios del dibujo por tolerancias del PDF Adcole.
            if err:db.execute('UPDATE pieces SET error=? WHERE id=?',(err,piece));failures+=1
            batch=[]
            for family,rows in parsed.items():
                for r in rows:
                    batch.append((piece,filename,line,family,str(r['Leva']) if r['Leva'] is not None else None,r['Apoyo'],r['Caracteristica'],None if pd.isna(r['Medicion']) else float(r['Medicion']),r['Area'],r['Resultado'],None if pd.isna(r['Min']) else float(r['Min']),None if pd.isna(r['Max']) else float(r['Max'])))
            if batch:db.executemany('INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',batch)
            db.execute('UPDATE pieces SET nrows=? WHERE id=?',(len(batch),piece))
            if not batch and not err:db.execute('UPDATE pieces SET error=? WHERE id=?',('No se extrajeron mediciones',piece));failures+=1
        except Exception as exc:
            db.execute('INSERT OR IGNORE INTO pieces(filename,line,gage,digest,error) VALUES(?,?,?,?,?)',(filename,'Sin identificar','',digest,str(exc)))
            failures+=1
        finally:
            del blob
        if done%25==0:db.commit();gc.collect()
        if progress and (done%10==0 or done==total):progress(done,total,filename)
    db.commit()
    return {'total':total,'processed':done,'duplicates':duplicates,'failures':failures}

def where(scope,alias=''):
    prefix=(alias+'.') if alias else ''
    return ('','()') if scope=='Acumulado' else (f' WHERE {prefix}line=?',(scope,))

def scoped(db,sql,scope,params=()):
    # Use placeholder {filter} to append to WHERE or AND without SQL injection.
    clause='1=1' if scope=='Acumulado' else 'line=?'
    p=() if scope=='Acumulado' else (scope,)
    return pd.read_sql_query(sql.replace('{filter}',clause),db,params=p+tuple(params))

def piece_status(db,scope):
    return scoped(db,'''SELECT p.id AS Pieza,p.filename AS "Nombre del archivo",p.line AS "Línea",p.gage AS "Gage ID",
      CASE WHEN EXISTS(SELECT 1 FROM measurements m WHERE m.piece=p.id AND m.family!='Chatter Journal Ford' AND m.result='Nok') THEN 'Nok' ELSE 'Ok' END AS Resultado,
      (SELECT COUNT(*) FROM measurements m WHERE m.piece=p.id AND m.family!='Chatter Journal Ford' AND m.result='Nok') AS "Mediciones NOK"
      FROM pieces p WHERE {filter} AND p.nrows>0 ORDER BY p.id''',scope)

def metrics(db,scope):
    ps=piece_status(db,scope)
    n=len(ps);bad=int(ps.Resultado.eq('Nok').sum()) if n else 0;good=n-bad
    rows=scoped(db,"SELECT COUNT(*) AS total, SUM(CASE WHEN result='Nok' THEN 1 ELSE 0 END) AS nok FROM measurements WHERE {filter} AND family!='Chatter Journal Ford'",scope).iloc[0]
    total=int(rows['total'] or 0); nok=int(rows['nok'] or 0)
    m=[('Total de piezas analizadas',n),('Piezas con resultado OK (todas las caracterist. OK)',good),('Piezas con resultado NOK (al menos una caracterist. NOK)',bad),('Porcentaje de Rechazo',round(100*bad/n,2) if n else 0),('FTQ (First Time Quality)',round(100*good/n,2) if n else 0),('Total de características/mediciones analizadas',total),('Características con resultado OK',total-nok),('Características con resultado NOK',nok),('% Características OK',round(100*(total-nok)/total,2) if total else 0),('% Características NOK',round(100*nok/total,2) if total else 0),('--- DESGLOSE DE RECHAZOS POR COMPONENTE ---',None)]
    comps=[('Piezas con fallas en APOYOS (Total)',('Apoyos','Chatter apoyos')),('  -> Rechazo Dimensional (Pestaña Apoyos)',('Apoyos',)),('  -> Rechazo por Chatter (Pestaña Chatter apoyos)',('Chatter apoyos',)),('Piezas con fallas en LEVAS (Total)',('Levas','chatter Levas')),('  -> Rechazo Dimensional (Pestaña Levas)',('Levas',)),('  -> Rechazo por Chatter (Pestaña chatter Levas)',('chatter Levas',))]
    for label,families in comps:
        placeholders=','.join('?'*len(families))
        df=scoped(db,f"SELECT COUNT(DISTINCT piece) n FROM measurements WHERE {{filter}} AND result='Nok' AND family IN ({placeholders})",scope,families)
        m.append((label,int(df.iloc[0,0])))
    reg=scoped(db,"SELECT DISTINCT piece,area FROM measurements WHERE {filter} AND result='Nok' AND characteristic='(5) 301-400 UPR' AND family='chatter Levas'",scope)
    reg_ids=set(reg.piece)
    other=scoped(db,"SELECT DISTINCT piece,characteristic FROM measurements WHERE {filter} AND result='Nok' AND family!='Chatter Journal Ford'",scope)
    chars=other.groupby('piece').characteristic.agg(set).to_dict() if not other.empty else {}
    only={p for p in reg_ids if chars.get(p)=={'(5) 301-400 UPR'}}
    together=reg_ids-only
    m.extend([('--- DETALLE ESPECÍFICO REG 5 ---',None),('Cantidad piezas rechazadas SOLO por "(5) 301-400 UPR"',len(only)),('Cantidad piezas rechazadas por "(5) 301-400 UPR" Y otra característica',len(together)),('Cantidad piezas rechazadas SOLO por "(5) 301-400 UPR" en Lift Area',len(only & set(reg.loc[reg.area.eq('Lift Area'),'piece']))),('Cantidad piezas rechazadas SOLO por "(5) 301-400 UPR" en Base Circle',len(only & set(reg.loc[reg.area.eq('Base Circle'),'piece']))),('Total de mediciones NOK',nok),('Porcentaje de mediciones NOK',round(100*nok/total,2) if total else 0)])
    reg_summary=pd.DataFrame({'Indicador':['Piezas con REG 5 NOK','Solo REG 5','REG 5 + otras características','REG 5 NOK en Base Circle','REG 5 NOK en Lift Area'],'Valor':[len(reg_ids),len(only),len(together),reg.loc[reg.area.eq('Base Circle'),'piece'].nunique(),reg.loc[reg.area.eq('Lift Area'),'piece'].nunique()]})
    return pd.DataFrame(m,columns=['Metrica','Valor']),reg_summary,ps

def capability(db,scope):
    q='''SELECT area AS Area,characteristic AS Caracteristica,lsl AS Min,usl AS Max,
       COUNT(measurement) AS N, AVG(measurement) AS Media,
       SUM(measurement*measurement) AS squares,MIN(measurement) AS Mínimo,MAX(measurement) AS Máximo,
       SUM(CASE WHEN result='Nok' THEN 1 ELSE 0 END) AS NOK
       FROM measurements WHERE {filter} AND family!='Chatter Journal Ford' AND measurement IS NOT NULL
       GROUP BY area,characteristic,lsl,usl'''
    d=scoped(db,q,scope)
    if d.empty:return pd.DataFrame(columns=['Area','Caracteristica','N','Media','Desv. estándar','Mínimo','Máximo','Min','Max','NOK','% NOK','Pp','Ppk'])
    d['Desv. estándar']=np.sqrt(np.maximum(0,(d.squares-d.N*d.Media**2)/(d.N-1).replace(0,np.nan)))
    d['% NOK']=100*d.NOK/d.N
    def indexes(r):
        sd=r['Desv. estándar']; mean=r['Media'];lo=r['Min'];hi=r['Max']
        if not np.isfinite(sd) or sd<=0:return pd.Series([np.nan,np.nan])
        if pd.notna(lo) and pd.notna(hi):return pd.Series([(hi-lo)/(6*sd),min((hi-mean)/(3*sd),(mean-lo)/(3*sd))])
        if pd.notna(hi):return pd.Series([np.nan,(hi-mean)/(3*sd)])
        if pd.notna(lo):return pd.Series([np.nan,(mean-lo)/(3*sd)])
        return pd.Series([np.nan,np.nan])
    d[['Pp','Ppk']]=d.apply(indexes,axis=1)
    return d.drop(columns=['squares']).sort_values('% NOK',ascending=False)

def pareto_data(db,scope,column,top=15,area=None):
    allowed={'Caracteristica':'characteristic','Pieza':'piece','Area':'area'}
    field=allowed[column]
    clause='' if area is None else ' AND area=?'
    return scoped(db,f'''SELECT {field} AS Etiqueta,COUNT(DISTINCT piece) AS "Piezas NOK" FROM measurements
        WHERE {{filter}} AND result='Nok' AND family!='Chatter Journal Ford'{clause} GROUP BY {field} ORDER BY "Piezas NOK" DESC LIMIT ?''',scope,(() if area is None else (area,))+(top,))

def data_view(db,scope,char=None,area=None,result=None,search=None,limit=5000,offset=0,family=None):
    filters=['{filter}'];params=[]
    if char:filters.append('characteristic=?');params.append(char)
    if area:filters.append('area=?');params.append(area)
    if result:filters.append('result=?');params.append(result)
    if family:filters.append('family=?');params.append(family)
    else:filters.append("family!='Chatter Journal Ford'")
    if search:filters.append('(CAST(piece AS TEXT) LIKE ? OR filename LIKE ?)');params.extend([f'%{search}%',f'%{search}%'])
    return scoped(db,'''SELECT filename AS "Nombre del archivo",piece AS Pieza,line AS "Línea",leva AS Leva,apoyo AS Apoyo,
      characteristic AS Caracteristica,measurement AS Medicion,area AS Area,result AS Resultado,lsl AS Min,usl AS Max
      FROM measurements WHERE '''+' AND '.join(filters)+' ORDER BY piece LIMIT ? OFFSET ?',scope,params+[limit,offset])

def errors(db):return pd.read_sql_query('SELECT filename AS Archivo,line AS Línea,gage AS Gage,error AS Error FROM pieces WHERE error IS NOT NULL',db)

def export_excel(db,scope):
    """Excel compacto: solo hojas solicitadas; mediciones escritas en streaming."""
    from openpyxl import Workbook
    out=io.BytesIO();wb=Workbook(write_only=True)
    summary,reg,ps=metrics(db,scope);caps=capability(db,scope)
    def frame_sheet(name,df):
        ws=wb.create_sheet(name[:31]);ws.append(list(df.columns))
        for values in df.itertuples(index=False,name=None):
            ws.append([None if pd.isna(x) else x for x in values])
    for fam in ['Apoyos','Levas','chatter Levas','Chatter apoyos']:
        ws=wb.create_sheet(fam[:31]);ws.append(['Nombre del archivo','Pieza','Línea','Leva','Apoyo','Caracteristica','Medicion','Area','Resultado','Min','Max'])
        for r in db.execute("""SELECT filename,piece,line,leva,apoyo,characteristic,measurement,area,result,lsl,usl
            FROM measurements WHERE (line=? OR ?='Acumulado') AND family=? ORDER BY piece""",(scope,scope,fam)):
            ws.append(r)
    ws=wb.create_sheet('Nok');ws.append(['Nombre del archivo','Pieza','Línea','Leva','Apoyo','Caracteristica','Medicion','Area','Resultado','Min','Max'])
    for r in db.execute("""SELECT filename,piece,line,leva,apoyo,characteristic,measurement,area,result,lsl,usl
            FROM measurements WHERE (line=? OR ?='Acumulado') AND family!='Chatter Journal Ford' AND result='Nok' ORDER BY piece""",(scope,scope)):
        ws.append(r)
    frame_sheet('Resumen ejecutivo',summary)
    frame_sheet('Resumen REG 5',reg)
    frame_sheet('Pp_Ppk',caps)
    wb.save(out);out.seek(0);return out.getvalue()

def drawing_limits():
    from engicom_core import AS,LS,CL,CA,CF
    rows=[]
    for support, chars in AS.items():
        for ch,(lo,hi) in chars.items():rows.append(('Apoyos',support,ch,lo,hi,'Código original / dibujo'))
    for ch,(lo,hi) in LS.items():rows.append(('Levas','',ch,lo,hi,'Código original / dibujo'))
    for fam,lims in [('Chatter levas',CL),('Chatter apoyos',CA),('Chatter Journal Ford',CF)]:
        for ch,hi in lims.items():rows.append((fam,'',ch,0,hi,'Código original / dibujo'))
    return pd.DataFrame(rows,columns=['Familia','Apoyo','Característica','LSL','USL','Fuente'])

def export_pdf(db,scope,topn=15):
    """Reporte completo de indicadores; distribuciones se construyen por característica con consulta limitada."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape,A4
    from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,Image,PageBreak
    from reportlab.lib.units import mm
    from reportlab.lib.enums import TA_CENTER
    from datetime import datetime
    out=io.BytesIO();doc=SimpleDocTemplate(out,pagesize=landscape(A4),rightMargin=12*mm,leftMargin=12*mm,topMargin=10*mm,bottomMargin=10*mm)
    styles=getSampleStyleSheet();story=[Paragraph(f'ENGICOM | REPORTE DE CALIDAD NoK | {scope}',styles['Title']),Paragraph(datetime.now().strftime('%Y-%m-%d %H:%M'),styles['Normal']),Spacer(1,4*mm)]
    summary,reg,ps=metrics(db,scope);caps=capability(db,scope)
    def table(df,limit=None):
        if df.empty:return
        df=df if limit is None else df.head(limit)
        data=[list(map(str,df.columns))]+[[str(v)[:52] if pd.notna(v) else '' for v in row] for row in df.itertuples(index=False,name=None)]
        t=Table(data,repeatRows=1,hAlign='LEFT');t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#15324B')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('FONTSIZE',(0,0),(-1,-1),7),('GRID',(0,0),(-1,-1),.25,colors.lightgrey)]));story.extend([t,Spacer(1,4*mm)])
    def figadd(fig):
        b=io.BytesIO();fig.savefig(b,format='png',dpi=100,bbox_inches='tight');plt.close(fig);b.seek(0);story.append(Image(b,width=230*mm,height=95*mm));story.append(Spacer(1,3*mm))
    story.append(Paragraph('1. Resumen ejecutivo',styles['Heading2']));table(summary)
    story.append(Paragraph('2. REG 5',styles['Heading2']));table(reg)
    for col in ['Caracteristica','Pieza','Area']:
        d=pareto_data(db,scope,col,topn)
        fig,ax=plt.subplots(figsize=(10,4));ax.barh(d.Etiqueta.astype(str),d['Piezas NOK'],color='#137C8B');ax.invert_yaxis();ax.set_title(f'Pareto NOK por {col}');fig.tight_layout();figadd(fig)
    areas=scoped(db,"SELECT DISTINCT area FROM measurements WHERE {filter} AND family!='Chatter Journal Ford' ORDER BY area",scope)
    for area in areas.area:
        d=pareto_data(db,scope,'Caracteristica',topn,area)
        if d.empty:continue
        fig,ax=plt.subplots(figsize=(10,4));ax.barh(d.Etiqueta.astype(str),d['Piezas NOK'],color='#137C8B');ax.invert_yaxis();ax.set_title(f'Pareto por característica · {area}');fig.tight_layout();figadd(fig)
    story.append(Paragraph('3. Pp / Ppk',styles['Heading2']));table(caps,50)
    valid=caps.dropna(subset=['Ppk']).copy()
    if not valid.empty:
        valid=valid.sort_values('Ppk').head(40)
        fig,ax=plt.subplots(figsize=(10,max(4,min(9,.24*len(valid)+1))))
        labels=(valid.Area.astype(str)+' · '+valid.Caracteristica.astype(str)).tolist()
        ax.barh(labels,valid.Ppk,color='#137C8B');ax.axvline(1.33,color='#E09F3E',linestyle='--');ax.invert_yaxis();ax.set_title('Ppk por área y característica');fig.tight_layout();figadd(fig)
    # One chart per characteristic, values sampled only for visualization, never for summary/FTQ/capability.
    chars=scoped(db,"SELECT DISTINCT characteristic FROM measurements WHERE {filter} AND family!='Chatter Journal Ford' ORDER BY characteristic",scope)
    story.append(Paragraph('4. Distribuciones por característica',styles['Heading2']))
    story.append(Paragraph('Gráficas: hasta 3,000 mediciones por característica para limitar memoria. Resúmenes y Pp/Ppk utilizan todas las mediciones.',styles['Italic']))
    for ch in chars.characteristic:
        d=data_view(db,scope,char=ch,limit=3000).dropna(subset=['Medicion'])
        if d.empty:continue
        fig,axes=plt.subplots(1,2,figsize=(10,4));groups=[g.Medicion.values for _,g in d.groupby('Area')]
        labels=list(d.groupby('Area').groups)
        if groups:axes[0].boxplot(groups,tick_labels=labels);axes[0].tick_params(axis='x',rotation=20)
        axes[0].set_title('Distribución por área');axes[1].hist(d.Medicion,bins=30,color='#137C8B');axes[1].set_title('Histograma')
        fig.suptitle(str(ch));fig.tight_layout();figadd(fig)
    story.append(Paragraph('Pp/Ppk son descriptivos y requieren validar estabilidad y límites de ingeniería vigentes.',styles['Italic']))
    doc.build(story);out.seek(0);return out.getvalue()


def point_filter_options(db, scope, family=None, characteristic=None):
    """Opciones calculadas en SQLite sin leer el conjunto completo."""
    clauses=[]; args=[]
    if scope!='Acumulado':clauses.append('line=?');args.append(scope)
    if family:clauses.append('family=?');args.append(family)
    if characteristic:clauses.append('characteristic=?');args.append(characteristic)
    sql=' WHERE '+' AND '.join(clauses) if clauses else ''
    if not family:
        return [r[0] for r in db.execute('SELECT DISTINCT family FROM measurements'+sql+' ORDER BY family',args) if r[0]]
    if not characteristic:
        return [r[0] for r in db.execute('SELECT DISTINCT characteristic FROM measurements'+sql+' ORDER BY characteristic',args) if r[0]]
    dimension='leva' if family in ('Levas','chatter Levas') else 'apoyo'
    return [r[0] for r in db.execute(f'SELECT DISTINCT {dimension} FROM measurements'+sql+f' AND {dimension} IS NOT NULL ORDER BY {dimension}' if clauses else f'SELECT DISTINCT {dimension} FROM measurements WHERE {dimension} IS NOT NULL ORDER BY {dimension}',args)]


def point_series(db,scope,family,characteristic,element=None,limit=2500,offset=0):
    dimension='leva' if family in ('Levas','chatter Levas') else 'apoyo'
    conditions=['family=?','characteristic=?','measurement IS NOT NULL'];args=[family,characteristic]
    if scope!='Acumulado':conditions.append('line=?');args.append(scope)
    if element is not None:conditions.append(f'{dimension}=?');args.append(str(element))
    where=' AND '.join(conditions)
    count=db.execute('SELECT COUNT(*) FROM measurements WHERE '+where,args).fetchone()[0]
    sql='''SELECT piece AS Pieza,filename AS Archivo,line AS Linea,leva AS Leva,apoyo AS Apoyo,
      measurement AS Valor,result AS Resultado,lsl AS LSL,usl AS USL,area AS Area
      FROM measurements WHERE '''+where+' ORDER BY piece,rowid LIMIT ? OFFSET ?'
    return pd.read_sql_query(sql,db,params=args+[int(limit),int(offset)]),count
