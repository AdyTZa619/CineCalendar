# Limitări rămase

- **EXE Windows necompilat în acest mediu.** Mediul de lucru este Linux fără toolchain Windows/PyInstaller și fără acces extern din container. Există build reproducibil pentru Windows și workflow GitHub Actions, dar nu este corect să pretind că un `.exe` a fost compilat și pornit aici.
- **Catalogul complet nu este inclus.** Exportul IMDb al utilizatorului conține filme văzute, nu candidați nevăzuți. Aplicația poate importa sute de mii/milioane de titluri din IMDb datasets oficiale sau un catalog CSV; nu se livrează o bază hardcodată mică.
- **Semantică fără enrichment.** Cu doar `title.basics` + `title.ratings`, semantica este bazată pe titlu/gen/deceniu/runtime. Pentru overview/keywords/țări/regizori/poster este necesar catalog CSV îmbogățit sau TMDb; aplicația are acum enrichment TMDb în batch, dar necesită tokenul real al utilizatorului.
- **TMDb branding.** Textul de attribution este prezent, dar logo-ul oficial aprobat TMDb nu este inclus în pachetul sursă. Pentru distribuirea unei versiuni cu TMDb activ trebuie adăugat un logo oficial aprobat conform termenilor TMDb.
- **IMDb datasets pot fi mari.** Importul lor poate dura și ocupă spațiu; este streaming și filtrează implicit titlurile fără minimum 50 voturi.
- **Updater.** Nu este implementat și rămâne dezactivat.
- **Poster offline.** Posterul este disponibil offline numai după ce a fost descărcat și cache-uit.
- **Test „mediu Windows curat”.** Este definit în build script ca smoke launch, dar nu a putut fi executat în mediul Linux curent.

- **Acceptance recomandări pe exportul real.** Exportul furnizat are 2.434 titluri, toate evaluate, deci are 0 candidați nevăzuți. Nu am fabricat un catalog ca să forțez trei recomandări; acest ultim test trebuie rerulat după importul unui catalog legitim.


### 0.2.0
Prima inițializare a catalogului necesită internet. Dataseturile IMDb de bază nu includ ploturi/postere/keywords; pentru semantică și postere mai bogate se poate activa TMDb cu tokenul utilizatorului.
