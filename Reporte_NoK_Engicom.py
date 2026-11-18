# -*- coding: utf-8 -*-
"""Reporte NoK ENGICOM V10: Línea 1, Línea 2 y Acumulado, persistencia SQLite."""
import os, tempfile, gc, uuid
from pathlib import Path
import pandas as pd
import numpy as np
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from engicom_backend import (initialize,process,metrics,capability,pareto_data,data_view,errors,export_excel,export_pdf,SCOPES)

st.set_page_config(page_title='Reporte NoK | ENGICOM',page_icon='📊',layout='wide')
st.markdown('''<style>.block-container{padding-top:1.1rem;max-width:1600px}[data-testid="stMetric"]{background:#f2f6fa;border:1px solid #dce5ee;padding:14px;border-radius:12px}h1,h2,h3{letter-spacing:-.02em}</style>''',unsafe_allow_html=True)

def safe_df(df,**kwargs):
    d=df.copy()
    for c in d.columns:
        if d[c].dtype=='object':d[c]=d[c].map(lambda v: '' if v is None or (isinstance(v,float) and pd.isna(v)) else str(v))
    st.dataframe(d,**kwargs)

def pfig(d,title):
    fig=go.Figure()
    if d.empty:fig.add_annotation(text='Sin registros NOK',xref='paper',yref='paper',x=.5,y=.5,showarrow=False)
    else:
        fig.add_bar(x=d.Etiqueta.astype(str),y=d['Piezas NOK'],name='Piezas NOK',marker_color='#137C8B')
        d=d.copy();d['Acumulado %']=100*d['Piezas NOK'].cumsum()/d['Piezas NOK'].sum()
        fig.add_scatter(x=d.Etiqueta.astype(str),y=d['Acumulado %'],name='% acumulado',yaxis='y2',mode='lines+markers',line=dict(color='#E09F3E',width=3))
    fig.update_layout(title=title,height=420,yaxis_title='Piezas únicas NOK',yaxis2=dict(title='% acumulado',overlaying='y',side='right',range=[0,105]),legend=dict(orientation='h',y=1.12),margin=dict(t=70,b=80))
    return fig

def main():
    st.title('📊 Reporte NoK | ENGICOM')
    st.caption('Calidad dimensional y chatter · Línea 1 / Línea 2 / Acumulado · Pareto · distribución · Pp/Ppk · detalle por pieza · Excel y PDF')
    with st.sidebar:
        st.header('Entrada de datos')
        files=st.file_uploader('PDF y/o ZIP',type=['pdf','zip'],accept_multiple_files=True)
        st.caption('Para 2,500 PDF, comprímelos en ZIP. Se guardan temporalmente en disco y se procesan uno por uno.')
        g1=st.text_input('Gage ID asignado a Línea 1','8377')
        g2=st.text_input('Gage ID asignado a Línea 2','8375')
        st.caption('La leyenda explícita "Line 2" tiene prioridad. Equipos desconocidos se clasifican como Sin identificar.')
        topn=st.slider('Elementos en Pareto',5,30,15)
        if st.button('🗑️ Limpiar resultados de esta sesión'):
            st.session_state.pop('engicom_db',None);st.session_state.pop('engicom_exports',None);st.rerun()
    if files and st.button('🚀 Procesar reportes',type='primary',width='stretch'):
        old=st.session_state.pop('engicom_db',None)
        st.session_state.pop('engicom_exports',None)
        if old:
            try:os.remove(old)
            except OSError:pass
        folder=Path(tempfile.gettempdir())/('engicom_'+uuid.uuid4().hex)
        folder.mkdir(parents=True,exist_ok=True)
        paths=[]
        for f in files:
            path=folder/Path(f.name).name
            with path.open('wb') as out:
                f.seek(0)
                while True:
                    block=f.read(1024*1024)
                    if not block:break
                    out.write(block)
            paths.append(path)
        dbpath=str(folder/'data.sqlite');db=initialize(dbpath)
        progress=st.progress(0);status=st.empty()
        def update(done,total,name):
            progress.progress(min(done/max(total,1),1));status.write(f'Procesando {done:,}/{total:,}: {name}')
        try:
            stats=process(paths,db,g1,g2,progress=update)
            st.session_state['engicom_db']=dbpath
            st.success(f"Reportes leídos: {stats['processed']:,} · Duplicados: {stats['duplicates']:,} · Incidencias: {stats['failures']:,}")
        except Exception as exc:st.error(f'Error al procesar: {exc}')
        finally:
            db.close()
            for p in paths:
                try:p.unlink()
                except OSError:pass
            gc.collect()
    path=st.session_state.get('engicom_db')
    if not path or not os.path.exists(path):
        st.info('Carga reportes PDF individuales o un ZIP para comenzar. El análisis distingue Línea 1, Línea 2 y Acumulado.')
        return
    db=initialize(path)
    try:
        counts=pd.read_sql_query('SELECT line AS Línea,COUNT(*) AS Piezas FROM pieces WHERE nrows>0 GROUP BY line',db)
        st.subheader('Identificación de línea');safe_df(counts,width='stretch',hide_index=True)
        scope=st.radio('Vista de resultados',SCOPES,horizontal=True,index=0)
        summary,reg,ps=metrics(db,scope)
        caps=capability(db,scope)
        pieces=len(ps);bad=int(ps.Resultado.eq('Nok').sum());good=pieces-bad
        a,b,c,e,f=st.columns(5)
        a.metric('Piezas analizadas',f'{pieces:,}');b.metric('Piezas OK',f'{good:,}');c.metric('Piezas NOK',f'{bad:,}');e.metric('FTQ',f'{100*good/pieces:.2f}%' if pieces else '—')
        f.metric('Mediciones NOK',f"{int(summary.loc[summary.Metrica.eq('Total de mediciones NOK'),'Valor'].iloc[0]):,}")
        tabs=st.tabs(['🏁 Ejecutivo','🎯 Pareto','📦 Distribución','📐 Pp / Ppk','🔎 Reportes individuales','📤 Exportar'])
        with tabs[0]:
            l,r=st.columns([1,1.2])
            with l:
                fig=go.Figure(data=[go.Pie(labels=['OK','NOK'],values=[good,bad],hole=.62,marker_colors=['#159A80','#D9485F'])]);fig.update_layout(title='Resultado por pieza',height=360,margin=dict(t=55,b=10));st.plotly_chart(fig,width='stretch')
            with r:
                st.subheader('Resumen ejecutivo');safe_df(summary,width='stretch',hide_index=True)
                st.subheader('REG 5 · (5) 301–400 UPR');safe_df(reg,width='stretch',hide_index=True)
            st.subheader('Piezas con más mediciones NOK');safe_df(ps.sort_values('Mediciones NOK',ascending=False).head(100),width='stretch',hide_index=True)
        with tabs[1]:
            for col in ['Caracteristica','Pieza']:
                st.plotly_chart(pfig(pareto_data(db,scope,col,topn),f'Pareto por {col} · Top {topn}'),width='stretch')
            areas=pd.read_sql_query('SELECT DISTINCT area FROM measurements ORDER BY area',db).area.tolist()
            area=st.selectbox('Área',areas)
            if area:st.plotly_chart(pfig(pareto_data(db,scope,'Caracteristica',topn,area),f'Pareto · {area}'),width='stretch')
        with tabs[2]:
            chars=pd.read_sql_query('SELECT DISTINCT characteristic FROM measurements ORDER BY characteristic',db).characteristic.tolist()
            ch=st.selectbox('Característica',chars)
            if ch:
                df=data_view(db,scope,char=ch,limit=10000).dropna(subset=['Medicion'])
                if not df.empty:
                    fig=px.box(df,x='Area',y='Medicion',color='Area',points='all',hover_data=['Pieza','Nombre del archivo'],title=f'Boxplot y valores individuales · {ch}')
                    for col,label,color in [('Min','LSL','#D97706'),('Max','USL','#DC2626')]:
                        z=df[col].dropna()
                        if not z.empty:fig.add_hline(y=z.iloc[0],line_dash='dash',line_color=color,annotation_text=label)
                    st.plotly_chart(fig,width='stretch')
                    st.plotly_chart(px.histogram(df,x='Medicion',color='Area',marginal='box',nbins=40,title=f'Histograma · {ch}',opacity=.75),width='stretch')
                    safe_df(df,width='stretch',hide_index=True)
                    if len(df)>=10000:st.caption('Vista limitada a 10,000 mediciones. Los indicadores y exportaciones usan el conjunto completo.')
        with tabs[3]:
            st.caption('Pp/Ppk calculados con desviación estándar global muestral. No demuestran estabilidad; valida los límites de ingeniería.')
            areas=['Todas']+sorted(caps.Area.dropna().unique().tolist()) if not caps.empty else ['Todas']
            af=st.selectbox('Filtrar área para capacidad',areas)
            cv=caps if af=='Todas' else caps[caps.Area.eq(af)]
            safe_df(cv.round(6),width='stretch',hide_index=True)
            valid=cv.dropna(subset=['Ppk']).copy()
            if not valid.empty:
                valid['Grupo']=valid.Area.astype(str)+' · '+valid.Caracteristica.astype(str)
                fig=px.bar(valid.sort_values('Ppk'),x='Ppk',y='Grupo',orientation='h',color='Ppk',color_continuous_scale='RdYlGn',title='Ppk por característica');fig.add_vline(x=1.33,line_dash='dash',line_color='#15324B');fig.update_layout(height=max(420,len(valid)*25));st.plotly_chart(fig,width='stretch')
        with tabs[4]:
            search=st.text_input('Buscar pieza o nombre de PDF');result=st.selectbox('Resultado',['Todos','Ok','Nok'])
            page=st.number_input('Página de resultados (1,000 mediciones por página)',min_value=1,value=1,step=1)
            v=data_view(db,scope,search=search,result=None if result=='Todos' else result,limit=1000,offset=(page-1)*1000)
            safe_df(v,width='stretch',hide_index=True,height=480)
            st.subheader('Registros NOK');safe_df(data_view(db,scope,result='Nok',limit=1000),width='stretch',hide_index=True,height=300)
        with tabs[5]:
            st.caption('Las exportaciones se generan bajo demanda. Incluyen la línea seleccionada y la columna Línea en Excel.')
            x,y=st.columns(2)
            if x.button('Preparar Excel completo'):
                with st.spinner('Generando Excel en modo escritura secuencial...'):
                    st.session_state['engicom_exports']={'scope':scope,'xlsx':export_excel(db,scope)}
            if y.button('Preparar dashboard PDF'):
                with st.spinner('Generando PDF ejecutivo...'):
                    st.session_state['engicom_exports']={'scope':scope,'pdf':export_pdf(db,scope,topn)}
            exp=st.session_state.get('engicom_exports',{})
            if exp.get('scope')==scope:
                if exp.get('xlsx'):st.download_button('⬇️ Descargar Excel completo',exp['xlsx'],file_name=f'Engicom_{scope.replace(" ","_")}.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                if exp.get('pdf'):st.download_button('⬇️ Descargar dashboard PDF',exp['pdf'],file_name=f'Engicom_{scope.replace(" ","_")}.pdf',mime='application/pdf')
            err=errors(db)
            if not err.empty:st.warning(f'{len(err)} reportes con incidencias');safe_df(err,width='stretch',hide_index=True)
    finally:db.close()

if __name__=='__main__':main()
