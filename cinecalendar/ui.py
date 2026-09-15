from __future__ import annotations
import os, threading, webbrowser, hashlib
from datetime import date, datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
try:
    from PIL import Image, ImageTk
except Exception:
    Image = ImageTk = None
import requests
from .backup import export_profile, import_profile
from .catalog import import_catalog_csv, import_imdb_datasets, bootstrap_official_imdb_catalog
from .feedback import apply_feedback
from .imdb_import import import_imdb_csv, add_manual_rating
from .profile import build_profile, get_profile, top_profile_features
from .recommendation import Recommendation, row_to_movie
from .tmdb import TmdbProvider, enrich_library
from .util import json_loads
from .watcher import RatingsFolderWatcher

COLORS={
"dark":{"bg":"#11151b","panel":"#171d25","card":"#202832","text":"#eef2f7","muted":"#9aa8b7","accent":"#6da8ff","danger":"#ff7a7a","border":"#2d3946","good":"#7fd39b"},
"light":{"bg":"#f4f6f8","panel":"#ffffff","card":"#ffffff","text":"#17202a","muted":"#647180","accent":"#276ef1","danger":"#c93838","border":"#dce2e8","good":"#198754"}
}

class ScrollFrame(ttk.Frame):
    def __init__(self,parent):
        super().__init__(parent)
        self.canvas=tk.Canvas(self,highlightthickness=0,borderwidth=0)
        self.scroll=ttk.Scrollbar(self,orient="vertical",command=self.canvas.yview)
        self.inner=ttk.Frame(self.canvas)
        self.inner.bind("<Configure>",lambda e:self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.window=self.canvas.create_window((0,0),window=self.inner,anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left",fill="both",expand=True); self.scroll.pack(side="right",fill="y")
        self.canvas.bind("<Configure>",lambda e:self.canvas.itemconfigure(self.window,width=e.width))
        self.canvas.bind_all("<MouseWheel>",self._wheel)
    def _wheel(self,e):
        try:self.canvas.yview_scroll(int(-1*(e.delta/120)),"units")
        except Exception:pass

class CineCalendarUI:
    def __init__(self,root,service):
        self.root=root; self.s=service; self.db=service.db; self.current=""
        self.root.title("CineCalendar")
        self.root.geometry("1280x820"); self.root.minsize(980,650)
        try:self.root.tk.call("tk","scaling",max(1.0,self.root.winfo_fpixels("1i")/96.0))
        except Exception:pass
        self.style=ttk.Style(); self.theme=self.db.get_setting("theme","dark"); self._apply_theme()
        self._build_shell(); self.show("today"); self._schedule_watch(); self.root.after(900,self._auto_catalog_if_needed)

    @property
    def c(self):return COLORS[self.theme]

    def _apply_theme(self):
        c=self.c; self.root.configure(bg=c["bg"])
        self.style.theme_use("clam")
        self.style.configure("TFrame",background=c["bg"]); self.style.configure("Panel.TFrame",background=c["panel"])
        self.style.configure("Card.TFrame",background=c["card"],relief="flat")
        self.style.configure("TLabel",background=c["bg"],foreground=c["text"],font=("Segoe UI",10))
        self.style.configure("Panel.TLabel",background=c["panel"],foreground=c["text"])
        self.style.configure("Card.TLabel",background=c["card"],foreground=c["text"])
        self.style.configure("Muted.Card.TLabel",background=c["card"],foreground=c["muted"],font=("Segoe UI",9))
        self.style.configure("Title.TLabel",background=c["bg"],foreground=c["text"],font=("Segoe UI Semibold",22))
        self.style.configure("Section.TLabel",background=c["bg"],foreground=c["text"],font=("Segoe UI Semibold",13))
        self.style.configure("CardTitle.TLabel",background=c["card"],foreground=c["text"],font=("Segoe UI Semibold",14))
        self.style.configure("Score.TLabel",background=c["card"],foreground=c["good"],font=("Segoe UI Semibold",13))
        self.style.configure("TButton",font=("Segoe UI",10),padding=(12,8))
        self.style.configure("Accent.TButton",font=("Segoe UI Semibold",10),padding=(12,8))
        self.style.map("Accent.TButton",background=[("active",c["accent"]),("!disabled",c["accent"])],foreground=[("!disabled","white")])
        self.style.configure("Side.TButton",font=("Segoe UI",10),padding=(16,11),anchor="w")
        self.style.configure("Treeview",font=("Segoe UI",9),rowheight=28,background=c["card"],fieldbackground=c["card"],foreground=c["text"])
        self.style.configure("Treeview.Heading",font=("Segoe UI Semibold",9))
        self.style.configure("TCheckbutton",background=c["bg"],foreground=c["text"])
        self.style.configure("TEntry",padding=7)

    def _build_shell(self):
        for w in self.root.winfo_children():w.destroy()
        shell=ttk.Frame(self.root); shell.pack(fill="both",expand=True)
        side=ttk.Frame(shell,style="Panel.TFrame",width=215); side.pack(side="left",fill="y"); side.pack_propagate(False)
        ttk.Label(side,text="CineCalendar",style="Panel.TLabel",font=("Segoe UI Semibold",18)).pack(anchor="w",padx=20,pady=(22,4))
        ttk.Label(side,text="calendar cinematografic personal",style="Panel.TLabel",foreground=self.c["muted"],font=("Segoe UI",8)).pack(anchor="w",padx=20,pady=(0,20))
        nav=[("today","Azi"),("calendar","Calendar"),("month","Programul lunii"),("profile","Profilul meu"),("ratings","Ratinguri IMDb"),("watchlist","Watchlist"),("history","Istoric recomandări"),("settings","Setări")]
        for key,label in nav:
            ttk.Button(side,text=label,style="Side.TButton",command=lambda k=key:self.show(k)).pack(fill="x",padx=10,pady=2)
        ttk.Frame(side,style="Panel.TFrame").pack(fill="both",expand=True)
        self.status_var=tk.StringVar(value="Pregătit")
        ttk.Label(side,textvariable=self.status_var,style="Panel.TLabel",foreground=self.c["muted"],wraplength=180).pack(fill="x",padx=18,pady=18)
        self.content=ttk.Frame(shell); self.content.pack(side="left",fill="both",expand=True)

    def _clear(self):
        for w in self.content.winfo_children():w.destroy()

    def show(self,key):
        self.current=key; self._clear()
        getattr(self,f"view_{key}")()

    def title(self,text,subtitle=None,actions=None):
        top=ttk.Frame(self.content); top.pack(fill="x",padx=28,pady=(24,16))
        left=ttk.Frame(top); left.pack(side="left",fill="x",expand=True)
        ttk.Label(left,text=text,style="Title.TLabel").pack(anchor="w")
        if subtitle:ttk.Label(left,text=subtitle,foreground=self.c["muted"]).pack(anchor="w",pady=(3,0))
        if actions:
            right=ttk.Frame(top); right.pack(side="right")
            for label,cmd,accent in actions:ttk.Button(right,text=label,command=cmd,style="Accent.TButton" if accent else "TButton").pack(side="left",padx=4)

    def card(self,parent,padx=18,pady=14):
        outer=ttk.Frame(parent,style="Card.TFrame",padding=(padx,pady)); outer.pack(fill="x",pady=7); return outer

    def _catalog_count(self):
        with self.db.connect() as con:
            total=con.execute("SELECT COUNT(*) FROM movies").fetchone()[0]; rated=con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0]
        return total,rated,total-rated

    def view_today(self):
        self.title("Azi",date.today().strftime("%d.%m.%Y"),[("Recalculează",lambda:self.show("today"),True)])
        sf=ScrollFrame(self.content); sf.pack(fill="both",expand=True,padx=28,pady=(0,22)); sf.canvas.configure(bg=self.c["bg"])
        events=self.s.calendar.relevant_events(date.today()); phase=self.s.calendar.season_phase(date.today())[0]
        info=self.card(sf.inner)
        ttk.Label(info,text=f"Context: {phase}",style="CardTitle.TLabel").pack(anchor="w")
        if events:
            ttk.Label(info,text=" • ".join(f"{e.name}" for e,_ in events[:3]),style="Muted.Card.TLabel").pack(anchor="w",pady=(5,0))
        total,rated,candidates=self._catalog_count()
        if candidates<=0:
            warn=self.card(sf.inner)
            ttk.Label(warn,text="Catalogul de recomandări nu este încă pregătit.",style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(warn,text=f"Ai {rated} titluri evaluate. CineCalendar poate descărca și construi singur catalogul oficial IMDb; nu trebuie să adaugi filme manual.",style="Muted.Card.TLabel",wraplength=900).pack(anchor="w",pady=6)
            ttk.Button(warn,text="Pregătește catalogul automat",command=lambda:self._bootstrap_catalog(False),style="Accent.TButton").pack(anchor="w",pady=(8,0)); return
        recs=self.s.recommender.recommend(date.today(),3,record=False)
        if not recs:
            ttk.Label(sf.inner,text="Nu am găsit 3 titluri eligibile după filtre. Verifică catalogul și filtrul Romance.").pack(anchor="w"); return
        self._record_once(recs,date.today(),"today")
        for i,rec in enumerate(recs,1):self.recommendation_card(sf.inner,rec,i)

    def _record_once(self,recs,ctx,slot):
        now=datetime.now().astimezone().isoformat(timespec="seconds")
        with self.db.tx() as con:
            for r in recs:
                exists=con.execute("SELECT 1 FROM recommendation_history WHERE movie_id=? AND context_date=? AND slot=?",(r.movie.id,ctx.isoformat(),slot)).fetchone()
                if not exists:con.execute("INSERT INTO recommendation_history(movie_id,recommended_at,context_date,slot,final_score) VALUES(?,?,?,?,?)",(r.movie.id,now,ctx.isoformat(),slot,r.score.final))

    def recommendation_card(self,parent,rec:Recommendation,index:int|None=None):
        m,s=rec.movie,rec.score; f=self.card(parent)
        body=ttk.Frame(f,style="Card.TFrame"); body.pack(fill="x")
        poster=ttk.Label(body,text="Poster\nindisponibil",style="Muted.Card.TLabel",anchor="center",width=16)
        poster.pack(side="left",fill="y",padx=(0,16),anchor="n")
        if m.poster_url:self._load_poster_async(poster,m.poster_url,m.imdb_id or str(m.id))
        right=ttk.Frame(body,style="Card.TFrame"); right.pack(side="left",fill="both",expand=True)
        header=ttk.Frame(right,style="Card.TFrame"); header.pack(fill="x")
        ttl=(f"{index}. " if index else "")+m.title+(f" ({m.year})" if m.year else "")
        ttk.Label(header,text=ttl,style="CardTitle.TLabel").pack(side="left",anchor="w")
        ttk.Label(header,text=f"{round(s.final*100)}% potrivire",style="Score.TLabel").pack(side="right")
        meta=[]
        if m.imdb_rating is not None:meta.append(f"IMDb {m.imdb_rating:.1f}")
        if m.runtime_min:meta.append(f"{m.runtime_min} min")
        if m.genres:meta.append(", ".join(m.genres[:4]))
        if m.directors:meta.append("Regia: "+", ".join(m.directors[:2]))
        ttk.Label(right,text="  •  ".join(meta) or "Metadate limitate",style="Muted.Card.TLabel").pack(anchor="w",pady=(5,10))
        ttk.Label(right,text="De ce pentru tine: "+s.personal_reason,style="Card.TLabel",wraplength=820).pack(anchor="w",pady=2)
        ttk.Label(right,text="De ce acum: "+s.calendar_reason,style="Card.TLabel",wraplength=820).pack(anchor="w",pady=2)
        btn=ttk.Frame(right,style="Card.TFrame"); btn.pack(fill="x",pady=(12,0))
        actions=[("Am văzut","seen"),("Vreau să văd","want_to_watch"),("Nu mă interesează","not_interested"),("Nu-mi recomanda filme de genul acesta","never_similar"),("Mai multe ca acesta","more_like_this"),("Mai puține ca acesta","less_like_this")]
        for label,kind in actions:
            ttk.Button(btn,text=label,command=lambda k=kind,mid=m.id:self._feedback(mid,k)).pack(side="left",padx=(0,5),pady=2)
        ttk.Button(btn,text="De ce mi-ai recomandat asta?",command=lambda r=rec:self._explain(r)).pack(side="right",pady=2)
        if m.imdb_id:
            ttk.Button(btn,text="IMDb",command=lambda iid=m.imdb_id:webbrowser.open(f"https://www.imdb.com/title/{iid}/")).pack(side="right",padx=5)

    def _load_poster_async(self,label,url,key):
        if Image is None or ImageTk is None:return
        cache=self.s.paths.cache/"posters";cache.mkdir(parents=True,exist_ok=True)
        ext=".jpg"; path=cache/(hashlib.sha256((key+url).encode()).hexdigest()+ext)
        def worker():
            try:
                if not path.exists():
                    r=requests.get(url,timeout=12);r.raise_for_status();path.write_bytes(r.content)
                img=Image.open(path).convert("RGB");img.thumbnail((120,180)); photo=ImageTk.PhotoImage(img)
                def apply():
                    if label.winfo_exists():label.configure(image=photo,text="");label.image=photo
                self.root.after(0,apply)
            except Exception:
                pass
        threading.Thread(target=worker,daemon=True).start()

    def _feedback(self,movie_id,kind):
        try:
            apply_feedback(self.db,movie_id,kind); self.status_var.set("Feedback salvat; profil recalculat.")
            if self.current in {"today","month","watchlist"}:self.root.after(80,lambda:self.show(self.current))
        except Exception as e:messagebox.showerror("Feedback",str(e))

    def _explain(self,rec):
        win=tk.Toplevel(self.root); win.title("Explicația scorului"); win.geometry("650x500"); win.configure(bg=self.c["bg"])
        ttk.Label(win,text=rec.movie.title,style="Title.TLabel").pack(anchor="w",padx=20,pady=(20,8))
        ttk.Label(win,text=f"Scor final: {rec.score.final*100:.1f}%",style="Section.TLabel").pack(anchor="w",padx=20,pady=(0,12))
        for name,pts,reason in rec.score.contributions:
            row=ttk.Frame(win,style="Card.TFrame",padding=10); row.pack(fill="x",padx=20,pady=3)
            ttk.Label(row,text=f"{pts:+.1f}  {name}",style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(row,text=reason,style="Muted.Card.TLabel",wraplength=580).pack(anchor="w",pady=(2,0))

    def view_calendar(self):
        self.title("Calendar","Repere ortodoxe, seculare, istorice și sezoniere")
        sf=ScrollFrame(self.content); sf.pack(fill="both",expand=True,padx=28,pady=(0,22)); sf.canvas.configure(bg=self.c["bg"])
        year=date.today().year
        for ev in self.s.calendar.events_for_year(year):
            if ev.end < date.today():continue
            f=self.card(sf.inner,14,10); d=ev.start.strftime("%d.%m") if ev.start==ev.end else f"{ev.start:%d.%m}–{ev.end:%d.%m}"
            ttk.Label(f,text=f"{d}  {ev.name}",style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(f,text=f"{ev.category} • importanță {ev.importance:.2f} • teme: {', '.join(ev.themes.keys())}",style="Muted.Card.TLabel").pack(anchor="w",pady=(3,0))

    def view_month(self):
        self.title("Programul lunii","Câte 3 filme pentru fiecare interval ales automat de CalendarEngine",[("Recalculează",lambda:self.show("month"),True)])
        sf=ScrollFrame(self.content); sf.pack(fill="both",expand=True,padx=28,pady=(0,22)); sf.canvas.configure(bg=self.c["bg"])
        total,rated,cands=self._catalog_count()
        if cands<=0:
            ttk.Label(sf.inner,text="Importă mai întâi un catalog cu titluri nevăzute din Setări.").pack(anchor="w");return
        program=self.s.recommender.month_program(date.today(),3,record=False)
        for a,b,label,recs in program:
            ttk.Label(sf.inner,text=f"{a:%d}–{b:%d %B} — {label}",style="Section.TLabel").pack(anchor="w",pady=(16,4))
            for i,r in enumerate(recs,1):self.recommendation_card(sf.inner,r,i)

    def view_profile(self):
        p=get_profile(self.db)
        self.title("Profilul meu",f"Ce a învățat algoritmul din {p.get('rated_count',0)} ratinguri")
        sf=ScrollFrame(self.content); sf.pack(fill="both",expand=True,padx=28,pady=(0,22)); sf.canvas.configure(bg=self.c["bg"])
        summary=self.card(sf.inner); delta=p.get("mean_user_minus_imdb")
        ttk.Label(summary,text=f"Ratinguri analizate: {p.get('rated_count',0)}",style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(summary,text=f"Diferența medie față de IMDb: {delta:+.2f}" if delta is not None else "Diferență IMDb: date insuficiente",style="Muted.Card.TLabel").pack(anchor="w",pady=4)
        sections=[("Genuri","genre:"),("Teme","theme:"),("Regizori","director:"),("Decenii","decade:"),("Durată","runtime:"),("Popularitate","popularity:"),("Țări / cinematografii","country:")]
        for label,prefix in sections:
            ttk.Label(sf.inner,text=label,style="Section.TLabel").pack(anchor="w",pady=(18,5))
            items=top_profile_features(p,prefix,True,12)
            if not items:ttk.Label(sf.inner,text="Date insuficiente",foreground=self.c["muted"]).pack(anchor="w");continue
            f=self.card(sf.inner)
            for name,st in items:
                clean=name.split(":",1)[1]; pref=st["preference"]; mean=st.get("mean_rating"); count=st.get("count")
                ttk.Label(f,text=f"{clean}: {mean:.2f}/10  •  {count} titluri  •  preferință {pref:+.2f}" if mean else f"{clean}: preferință {pref:+.2f}",style="Card.TLabel").pack(anchor="w",pady=2)

    def view_ratings(self):
        self.title("Ratinguri IMDb","Import manual, rating instant și detectarea automată a exporturilor",[("Import IMDb ratings.csv",self._import_ratings,True),("Adaugă rating",self._manual_rating,False)])
        sf=ScrollFrame(self.content); sf.pack(fill="both",expand=True,padx=28,pady=(0,22)); sf.canvas.configure(bg=self.c["bg"])
        total,rated,cands=self._catalog_count(); f=self.card(sf.inner)
        ttk.Label(f,text=f"{rated} ratinguri locale • {total} titluri în DB • {cands} candidați nevăzuți",style="CardTitle.TLabel").pack(anchor="w")
        auto=tk.BooleanVar(value=bool(self.db.get_setting("auto_watch_enabled",False)))
        def toggle():self.db.set_setting("auto_watch_enabled",auto.get());self.status_var.set("Monitorizarea folderului a fost actualizată.")
        ttk.Checkbutton(f,text="Detectează automat exporturi IMDb noi",variable=auto,command=toggle).pack(anchor="w",pady=(8,3))
        ttk.Label(f,text="Folder: "+str(self.db.get_setting("ratings_folder",str(Path.home()/"Downloads"))),style="Muted.Card.TLabel").pack(anchor="w")
        ttk.Button(f,text="Scanează acum",command=self._scan_watch).pack(anchor="w",pady=(8,0))
        ttk.Label(sf.inner,text="Ultimele ratinguri",style="Section.TLabel").pack(anchor="w",pady=(18,5))
        tree=ttk.Treeview(sf.inner,columns=("date","title","rating","source"),show="headings",height=18)
        for c,w in (("date",110),("title",520),("rating",80),("source",100)):tree.heading(c,text=c.capitalize());tree.column(c,width=w,anchor="w")
        with self.db.connect() as con:
            rows=con.execute("SELECT r.date_rated,m.title,r.rating,r.source FROM ratings r JOIN movies m ON m.id=r.movie_id ORDER BY COALESCE(r.date_rated,'') DESC,r.id DESC LIMIT 300").fetchall()
        for r in rows:tree.insert("","end",values=(r["date_rated"] or "",r["title"],r["rating"],r["source"]))
        tree.pack(fill="both",expand=True)

    def _import_ratings(self):
        p=filedialog.askopenfilename(title="Selectează exportul IMDb",filetypes=[("CSV","*.csv"),("Toate fișierele","*.*")])
        if not p:return
        try:
            res=import_imdb_csv(self.db,p); build_profile(self.db)
            if res.skipped_same_file:messagebox.showinfo("IMDb","Acest fișier a fost deja importat (hash identic).")
            else:
                msg=f"Import: {res.total_rows} rânduri\nRatinguri noi: {len(res.new_ratings)}\nModificate: {len(res.changed_ratings)}\nReconciliate manual: {res.merged_manual}"
                if res.new_ratings:msg+="\n\nExemplu nou: "+f"{res.new_ratings[0][0]} — {res.new_ratings[0][1]}/10"
                messagebox.showinfo("Profil actualizat",msg)
            self.show("ratings")
            total,rated,candidates=self._catalog_count()
            if rated>0 and candidates<=0:
                self._bootstrap_catalog(False,auto=True)
        except Exception as e:self.s.log.exception("IMDb import failed");messagebox.showerror("Import IMDb",str(e))

    def _manual_rating(self):
        win=tk.Toplevel(self.root); win.title("Adaugă rating"); win.geometry("420x410"); win.configure(bg=self.c["bg"])
        fields={}
        for label,key in (("Titlu","title"),("An","year"),("Rating 1–10","rating"),("IMDb ID (opțional)","imdb"),("Genuri, separate prin virgulă","genres")):
            ttk.Label(win,text=label).pack(anchor="w",padx=24,pady=(12,3)); e=ttk.Entry(win);e.pack(fill="x",padx=24);fields[key]=e
        def save():
            try:
                genres=[x.strip() for x in fields["genres"].get().split(",") if x.strip()]
                add_manual_rating(self.db,fields["title"].get(),int(fields["year"].get()) if fields["year"].get().strip() else None,int(fields["rating"].get()),fields["imdb"].get(),genres)
                build_profile(self.db); win.destroy(); self.show("ratings")
            except Exception as e:messagebox.showerror("Rating",str(e),parent=win)
        ttk.Button(win,text="Salvează",style="Accent.TButton",command=save).pack(anchor="e",padx=24,pady=22)

    def _scan_watch(self):
        try:
            folder=self.db.get_setting("ratings_folder",str(Path.home()/"Downloads")); results=RatingsFolderWatcher(self.db,folder).scan()
            if results:
                r=results[0]
                build_profile(self.db)
                parts=[]
                if r.new_ratings:
                    title,rating=r.new_ratings[0]
                    parts.append(f"Rating nou detectat: {title} — {rating}/10" if len(r.new_ratings)==1 else f"{len(r.new_ratings)} ratinguri noi detectate (ex.: {title} — {rating}/10)")
                if r.changed_ratings:
                    title,old,new=r.changed_ratings[0]
                    parts.append(f"Rating modificat: {title} — {old}/10 → {new}/10" if len(r.changed_ratings)==1 else f"{len(r.changed_ratings)} ratinguri modificate (ex.: {title} — {old}/10 → {new}/10)")
                parts.append("Profil actualizat")
                self.status_var.set(" • ".join(parts));self.show("ratings")
            else:self.status_var.set("Nu există export IMDb nou în folderul monitorizat.")
        except Exception as e:self.s.log.exception("watch scan failed");self.status_var.set("Eroare la scanarea exporturilor IMDb.")

    def _schedule_watch(self):
        if self.db.get_setting("auto_watch_enabled",False):self._scan_watch()
        self.root.after(30000,self._schedule_watch)

    def view_watchlist(self):
        self.title("Watchlist","Titlurile marcate «Vreau să văd»")
        sf=ScrollFrame(self.content);sf.pack(fill="both",expand=True,padx=28,pady=(0,22));sf.canvas.configure(bg=self.c["bg"])
        with self.db.connect() as con:
            rows=con.execute("SELECT m.*,w.added_at FROM watchlist w JOIN movies m ON m.id=w.movie_id ORDER BY w.updated_at DESC").fetchall()
        if not rows:ttk.Label(sf.inner,text="Watchlist-ul este gol.").pack(anchor="w");return
        for row in rows:
            m=row_to_movie(row);f=self.card(sf.inner);ttk.Label(f,text=f"{m.title} ({m.year or '—'})",style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(f,text=", ".join(m.genres),style="Muted.Card.TLabel").pack(anchor="w",pady=3)

    def view_history(self):
        self.title("Istoric recomandări","Când a fost recomandat fiecare film și ce acțiune ai făcut")
        tree=ttk.Treeview(self.content,columns=("date","title","context","slot","score","action"),show="headings")
        for c,w in (("date",170),("title",360),("context",100),("slot",160),("score",80),("action",140)):tree.heading(c,text=c.capitalize());tree.column(c,width=w,anchor="w")
        with self.db.connect() as con:
            rows=con.execute("SELECT h.*,m.title FROM recommendation_history h JOIN movies m ON m.id=h.movie_id ORDER BY h.id DESC LIMIT 1500").fetchall()
        for r in rows:tree.insert("","end",values=(r["recommended_at"],r["title"],r["context_date"],r["slot"],f"{(r['final_score'] or 0)*100:.0f}%",r["action"] or ""))
        tree.pack(fill="both",expand=True,padx=28,pady=(0,28))

    def view_settings(self):
        self.title("Setări","Surse de metadate, filtre, backup și build")
        sf=ScrollFrame(self.content);sf.pack(fill="both",expand=True,padx=28,pady=(0,22));sf.canvas.configure(bg=self.c["bg"])
        f=self.card(sf.inner);ttk.Label(f,text="Preferințe",style="CardTitle.TLabel").pack(anchor="w")
        romance=tk.BooleanVar(value=bool(self.db.get_setting("exclude_romance",True)))
        ttk.Checkbutton(f,text="Exclude Romance (implicit ON)",variable=romance,command=lambda:self.db.set_setting("exclude_romance",romance.get())).pack(anchor="w",pady=7)
        th=tk.StringVar(value=self.theme); row=ttk.Frame(f,style="Card.TFrame");row.pack(fill="x",pady=5);ttk.Label(row,text="Temă",style="Card.TLabel").pack(side="left")
        cb=ttk.Combobox(row,textvariable=th,values=["dark","light"],state="readonly",width=12);cb.pack(side="left",padx=10)
        def change_theme(e=None):self.theme=th.get();self.db.set_setting("theme",self.theme);self._apply_theme();self._build_shell();self.show("settings")
        cb.bind("<<ComboboxSelected>>",change_theme)
        folder=self.card(sf.inner);ttk.Label(folder,text="Monitorizare IMDb CSV",style="CardTitle.TLabel").pack(anchor="w")
        folder_var=tk.StringVar(value=str(self.db.get_setting("ratings_folder",str(Path.home()/"Downloads"))))
        er=ttk.Entry(folder,textvariable=folder_var);er.pack(fill="x",pady=6)
        def choose_folder():
            p=filedialog.askdirectory(initialdir=folder_var.get());
            if p:folder_var.set(p);self.db.set_setting("ratings_folder",p)
        ttk.Button(folder,text="Alege folder",command=choose_folder).pack(anchor="w")
        cat=self.card(sf.inner);ttk.Label(cat,text="Catalog filme — automat",style="CardTitle.TLabel").pack(anchor="w")
        total,rated,candidates=self._catalog_count()
        ttk.Label(cat,text=f"Catalog local: {total:,} titluri • evaluate: {rated:,} • candidați nevăzuți: {max(0,candidates):,}. CineCalendar descarcă singur dataseturile oficiale IMDb și le păstrează în cache; nu trebuie să adaugi filme manual.",style="Muted.Card.TLabel",wraplength=950).pack(anchor="w",pady=5)
        rowcat=ttk.Frame(cat,style="Card.TFrame");rowcat.pack(fill="x",pady=6)
        ttk.Button(rowcat,text="Pregătește catalogul automat",command=lambda:self._bootstrap_catalog(False),style="Accent.TButton").pack(side="left",padx=(0,5))
        ttk.Button(rowcat,text="Actualizează catalogul de pe IMDb",command=lambda:self._bootstrap_catalog(True)).pack(side="left",padx=(0,5))
        ttk.Button(rowcat,text="Import manual (avansat)",command=self._import_imdb_dataset_dialog).pack(side="left")
        tm=self.card(sf.inner);ttk.Label(tm,text="TMDb — îmbogățire semantică opțională",style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(tm,text="Folosește API Read Access Token-ul tău pentru overview, keywords, țări, regizori și poster. Fără token, această funcție rămâne dezactivată, nu simulată.",style="Muted.Card.TLabel",wraplength=950).pack(anchor="w",pady=5)
        token_var=tk.StringVar(value=self.db.get_setting("tmdb_token","")); e=ttk.Entry(tm,textvariable=token_var,show="•");e.pack(fill="x",pady=5)
        def save_token():self.db.set_setting("tmdb_token",token_var.get().strip());self.status_var.set("Token TMDb salvat local.")
        btnrow=ttk.Frame(tm,style="Card.TFrame");btnrow.pack(fill="x",pady=(2,0))
        ttk.Button(btnrow,text="Salvează token",command=save_token).pack(side="left",padx=(0,5))
        ttk.Button(btnrow,text="Testează",command=lambda:self._test_tmdb(token_var.get())).pack(side="left",padx=(0,5))
        ttk.Button(btnrow,text="Îmbogățește 100 titluri",command=lambda:self._enrich_tmdb(token_var.get(),100)).pack(side="left")
        bk=self.card(sf.inner);ttk.Label(bk,text="Backup profil",style="CardTitle.TLabel").pack(anchor="w")
        ttk.Button(bk,text="Export profile",command=self._export_profile).pack(side="left",padx=(0,5),pady=6);ttk.Button(bk,text="Import profile",command=self._import_profile).pack(side="left",pady=6)
        up=self.card(sf.inner);ttk.Label(up,text="Updater",style="CardTitle.TLabel").pack(anchor="w");ttk.Label(up,text="Dezactivat. Arhitectura este pregătită, dar nu există un updater complet implementat și nu este simulat.",style="Muted.Card.TLabel").pack(anchor="w",pady=5)
        cr=self.card(sf.inner);ttk.Label(cr,text="Credite și surse",style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(cr,text="Information courtesy of IMDb (https://www.imdb.com). Used with permission.",style="Muted.Card.TLabel",wraplength=950).pack(anchor="w",pady=(5,2))
        ttk.Label(cr,text="This product uses the TMDB API but is not endorsed or certified by TMDB. Pentru distribuție cu TMDb activ, trebuie folosit și un logo oficial aprobat TMDb în secțiunea Credits.",style="Muted.Card.TLabel",wraplength=950).pack(anchor="w",pady=(2,5))

    def _auto_catalog_if_needed(self):
        try:
            total,rated,candidates=self._catalog_count()
            if rated>0 and candidates<=0 and not self.db.get_setting("catalog_bootstrap_running",False):
                self._bootstrap_catalog(False,auto=True)
        except Exception:
            self.s.log.exception("Automatic catalog check failed")

    def _bootstrap_catalog(self,force=False,auto=False):
        if self.db.get_setting("catalog_bootstrap_running",False):
            self.status_var.set("Catalogul este deja în curs de pregătire…"); return
        self.db.set_setting("catalog_bootstrap_running",True)
        self.status_var.set("Pregătesc automat catalogul oficial IMDb…")
        cache_dir=self.s.paths.cache/"imdb_datasets"
        def progress(msg):
            self.root.after(0,lambda t=msg:self.status_var.set(t))
        def worker():
            try:
                result=bootstrap_official_imdb_catalog(self.db,cache_dir,50,progress,force_download=force)
                build_profile(self.db)
                self.db.set_setting("catalog_initialized",True)
                self.db.set_setting("catalog_last_movies",result.get("movies",0))
                msg=f"Catalog pregătit: {result['movies']:,} titluri eligibile. Recomandările pot fi generate acum."
                self.root.after(0,lambda:self.status_var.set(msg))
                if not auto:self.root.after(0,lambda:messagebox.showinfo("Catalog CineCalendar",msg))
                self.root.after(0,lambda:self.show("today"))
            except Exception as e:
                self.s.log.exception("Automatic IMDb catalog bootstrap failed")
                msg="Nu am putut pregăti automat catalogul. Verifică internetul și încearcă din nou din Setări.\n\n"+str(e)
                self.root.after(0,lambda:self.status_var.set("Catalogul automat a eșuat; vezi mesajul de eroare."))
                self.root.after(0,lambda:messagebox.showerror("Catalog CineCalendar",msg))
            finally:
                self.db.set_setting("catalog_bootstrap_running",False)
        threading.Thread(target=worker,daemon=True).start()

    def _import_catalog(self):
        p=filedialog.askopenfilename(filetypes=[("CSV","*.csv")]);
        if not p:return
        try:r=import_catalog_csv(self.db,p);build_profile(self.db);messagebox.showinfo("Catalog",f"Adăugate: {r['added']}\nActualizate: {r['updated']}\nProfil recalculat.");self.show("settings")
        except Exception as e:messagebox.showerror("Catalog",str(e))

    def _import_imdb_dataset_dialog(self):
        basics=filedialog.askopenfilename(title="title.basics.tsv.gz",filetypes=[("GZip","*.gz")]);
        if not basics:return
        ratings=filedialog.askopenfilename(title="title.ratings.tsv.gz",filetypes=[("GZip","*.gz")]);
        if not ratings:return
        self.status_var.set("Import dataset IMDb în curs…")
        def worker():
            try:
                r=import_imdb_datasets(self.db,basics,ratings,50,lambda x:self.root.after(0,lambda t=x:self.status_var.set(t)))
                build_profile(self.db)
                self.root.after(0,lambda:messagebox.showinfo("IMDb dataset",f"Importate {r['movies']:,} titluri eligibile."))
                self.root.after(0,lambda:self.show("settings"))
            except Exception as e:self.s.log.exception("IMDb dataset import failed");self.root.after(0,lambda:messagebox.showerror("IMDb dataset",str(e)))
        threading.Thread(target=worker,daemon=True).start()

    def _test_tmdb(self,token):
        try:TmdbProvider(self.db,token).test_connection();messagebox.showinfo("TMDb","Conexiunea este validă.")
        except Exception as e:messagebox.showerror("TMDb",str(e))

    def _enrich_tmdb(self,token,limit=100):
        token=(token or "").strip()
        if not token:
            messagebox.showerror("TMDb","Introdu și salvează API Read Access Token-ul TMDb.");return
        self.db.set_setting("tmdb_token",token);self.status_var.set("Îmbogățire TMDb în curs…")
        def worker():
            try:
                r=enrich_library(self.db,token,limit,lambda x:self.root.after(0,lambda t=x:self.status_var.set(t)))
                build_profile(self.db)
                msg=f"Procesate: {r['requested']}\nÎmbogățite: {r['enriched']}\nFără rezultat nou: {r['missing']}\nErori: {r['failed']}"
                self.root.after(0,lambda:messagebox.showinfo("TMDb",msg))
                self.root.after(0,lambda:self.status_var.set("Metadata TMDb actualizate; profil recalculat."))
            except Exception as e:
                self.s.log.exception("TMDb enrichment failed");self.root.after(0,lambda:messagebox.showerror("TMDb",str(e)))
        threading.Thread(target=worker,daemon=True).start()

    def _export_profile(self):
        p=filedialog.asksaveasfilename(defaultextension=".zip",initialfile=f"CineCalendar-profile-{date.today().isoformat()}.zip",filetypes=[("ZIP","*.zip")]);
        if p:
            try:export_profile(self.db,p);messagebox.showinfo("Backup","Export finalizat.")
            except Exception as e:messagebox.showerror("Backup",str(e))

    def _import_profile(self):
        p=filedialog.askopenfilename(filetypes=[("ZIP","*.zip")]);
        if p:
            try:r=import_profile(self.db,p);messagebox.showinfo("Backup",f"Importate/îmbinate: {r}");self.show("settings")
            except Exception as e:messagebox.showerror("Backup",str(e))
