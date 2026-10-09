# Marker musicali per DaVinci Resolve

Il programma analizza un file audio ed esporta marker di un fotogramma in un file `.edl`, importabile nella timeline di Resolve. Può marcare ogni **tempo** oppure il primo tempo di ogni **misura**. I marker blu hanno evidenza concordante; quelli gialli, con suffisso `CHECK`, richiedono una verifica.

## Avvio

Richiede Docker. Inserire il brano in `input/music.mp3`, poi:

```bash
./build.sh
./start.sh
```

Il motore predefinito è madmom. Gli output sono:

- `output/music.edl`: marker per Resolve.
- `output/music.json`: tempi originali, correzioni, punteggi, motivi di esclusione e configurazione.
- `output/music-debug.png`: forma d'onda, attacchi e qualità dei marker.
- `output/music-clicks.wav`: musica con click sulle posizioni **esportate**, arrotondate ai fotogrammi.

Gli script funzionano anche se avviati da un'altra cartella. `MUSIC_FILE` cambia il nome del file dentro `input`; `MUSIC_IMAGE` cambia l'immagine Docker. Le opzioni aggiunte a `start.sh` sostituiscono i valori predefiniti.

## Beat This! e confronto tra modelli

È disponibile anche [Beat This!](https://github.com/CPJKU/beat_this), il modello Transformer pubblicato a ISMIR 2024. Il codice e i pesi sono distribuiti dagli autori con licenza MIT. Per installare il motore opzionale:

```bash
WITH_BEAT_THIS=1 ./build.sh
BEAT_BACKEND=ensemble ./start.sh
```

`ensemble` mantiene la sequenza di Beat This! e controlla la concordanza con madmom. I disaccordi vengono segnalati; le sequenze non vengono unite e i tempi non vengono mediati automaticamente. Per usare soltanto il Transformer:

```bash
BEAT_BACKEND=beat-this ./start.sh
```

Il primo utilizzo scarica il checkpoint `final0` (circa 78 MB) dal server pubblico degli autori. I pesi vengono conservati in `models/torch`, montato dallo script: gli avvii successivi possono funzionare offline. Per ridurre il carico usare `--checkpoint small0`; per un checkpoint locale, salvarlo in `models` e passare `--checkpoint /models/nome.ckpt`. Il funzionamento standard usa la CPU; su un'installazione Python nativa si possono selezionare `--device mps` o `--device cuda`, se disponibili. Il Docker fornito installa PyTorch CPU.

Il modello alternativo non garantisce risultati migliori su ogni brano. Il confronto con annotazioni umane resta necessario per misurare la qualità.

In modalità ensemble, un attacco debole può essere confermato soltanto se entrambe le reti lo sostengono e la distanza temporale è molto piccola (al massimo 20 ms). La soglia effettiva di forza viene registrata in ogni evento del JSON. Questo limita lo scarto automatico dei colpi quieti senza autorizzare grandi spostamenti verso rumore o note vicine.

## Scelta dei marker

```bash
# Ogni tempo, anziché soltanto il primo di ogni misura
./start.sh --marker-type beats

# Solo tempi con un attacco sonoro confermato vicino alla previsione
./start.sh --marker-type beats --mode attacks

# Escludere anche i marker che richiedono revisione
BEAT_BACKEND=ensemble ./start.sh --marker-type beats --mode attacks --strict

# Inizi di misura in un 4/4 noto, con suggerimento di tempo
./start.sh --beats-per-bar 4 --bpm 128
```

`--mode grid` mantiene le pulsazioni implicite: un tempo musicale può esistere anche durante una pausa o una sincope. `--mode attacks` privilegia i punti udibili utili al montaggio, sacrificando alcuni tempi validi privi di un transitorio chiaro. `--strict` è ancora più selettivo e può lasciare vuoti: non crea marker sostitutivi.

`--min-confidence 0.65` stabilisce la soglia per la revisione. Il punteggio combina attivazione del modello, attacco, regolarità locale e controlli disponibili; **non è una probabilità calibrata**. I segnali ambigui possono richiedere revisione anche con un punteggio alto. La soglia comporta esclusione soltanto con `--strict`.

Il numero originale di misura/tempo rimane nel nome del marker quando altri marker vengono esclusi. La numerazione degli eventi EDL è indipendente.

## Analisi e limiti

1. L'audio viene decodificato una sola volta, mantenendo la stessa origine temporale per tutti i motori. Il PCM comune è mono a 44.1 kHz; le trasformazioni interne dei modelli mantengono i loro sample rate previsti. In caso di forte cancellazione di fase stereo viene usato il canale più energico, con avviso nel JSON.
2. Gli attacchi vengono analizzati a 22.05 kHz con hop di 128 campioni (circa 5.8 ms), usando mix completo, componente percussiva HPSS e basse frequenze. Un passaggio sull'energia cerca il fronte del transitorio entro una finestra limitata. Questa risoluzione non equivale a una garanzia di accuratezza.
3. Le posizioni del modello possono spostarsi al massimo di `--max-snap-ms 50`, e comunque non oltre il 10% del periodo locale. Attacchi deboli, alternativi di forza simile, incroci e discontinuità ritmiche non autorizzano uno spostamento arbitrario. `--no-refine` conserva i tempi originali per il confronto.
4. L'energia molto bassa, le attivazioni deboli, l'assenza di attacchi, le irregolarità e i disaccordi tra reti producono indicatori di revisione. `--silence-db -60` controlla il limite assoluto; è applicato anche un limite relativo di -45 dB rispetto al 95° percentile dell'energia del brano. Per musica molto quieta questa impostazione va verificata.
5. Un controllo librosa confronta la fase degli attacchi usando il tempo locale della rete. È una verifica guidata, non un secondo riconoscimento indipendente del metro. Il controllo gira in un processo separato: un errore nativo o un timeout lascia disponibili i risultati della rete e un avviso. `--no-librosa-check` lo disattiva. Numba usa CPU `generic` per evitare istruzioni ARM non supportate in alcuni ambienti Docker.

La DBN di madmom considera per default misure di 3 o 4 tempi e 40–240 BPM. `--beats-per-bar 3,4`, `--min-bpm`, `--max-bpm`, `--transition-lambda` e `--activation-threshold` permettono di controllarla. Valori maggiori di `transition-lambda` favoriscono un tempo stabile; musica dal vivo può richiedere valori minori. La selezione automatica 3/4 della DBN sceglie un metro per l'intero file: non riconosce automaticamente ogni cambio di metro.

Beat This! usa normalmente il suo decoder minimale, che può produrre numeri variabili di tempi tra gli inizi di misura. `--beat-this-dbn` usa invece la DBN configurata. Specificare un metro, un BPM, limiti BPM o parametri DBN personalizzati attiva la DBN anche per Beat This!, per non ignorare i vincoli. `--bpm` restringe l'intervallo a ±15%, nei limiti di `--min-bpm`/`--max-bpm`; non forza una griglia a BPM esattamente costante. I tempi prima del primo downbeat restano classificati come introduzione/anacrusi, senza inventare un inizio di misura.

HPSS è un'indicazione ausiliaria, non una separazione perfetta della batteria; `--no-percussive` la disattiva. Sincopi, rubato, introduzioni ambient, mezzi/doppi tempi e cambi di metro richiedono ancora verifica musicale. Nessun marker è garantito corretto dal colore o dal punteggio.

## Posizione della clip e timecode

Gli FPS devono corrispondere alla timeline. Sono supportati FPS interi e 23.976, 29.97, 59.94, anche come frazioni esatte. L'aritmetica somma **fotogrammi** al timecode di partenza, distinguendo la frequenza effettiva dai numeri delle etichette; non introduce lo scarto di circa 3.6 secondi all'ora presente nella precedente conversione NTSC.

```bash
# Audio collocato dieci secondi dopo l'origine della timeline,
# con i primi 5 secondi del file rimossi e un offset manuale di un frame
./start.sh --fps 25 --timeline-start 01:00:00:00 \
  --audio-start 01:00:10:00 --source-in 5 --offset-ms 40

# NTSC non-drop-frame
./start.sh --fps 24000/1001

# Drop-frame: ammesso soltanto a 29.97 e 59.94 FPS
./start.sh --fps 29.97 --drop-frame --timeline-start '01:00:00;00'
```

`--audio-start` è il timecode **assoluto di inizio della clip già rifilata**; per default coincide con l'origine della timeline. `--source-in` e `--source-out` sono posizioni in secondi nel file sorgente. L'analisi usa comunque l'intero file per conservare il contesto ritmico. Un offset positivo ritarda i marker. Eventi fuori dalla clip e duplicati nello stesso fotogramma vengono esclusi e registrati nel JSON.

L'arrotondamento al fotogramma più vicino avviene una sola volta: a 25 fps resta un errore inevitabile fino a circa 20 ms. Il JSON registra `quantization_error_ms` e la posizione effettiva `export_source_seconds`. Le funzioni non implementano retiming della clip, cambi di velocità o mappature temporali non lineari. L'importazione in Resolve va controllata sulla timeline reale, soprattutto per drop-frame e inizio clip.

## Diagnostica locale

```bash
./start.sh --debug-start 30 --debug-duration 10
```

Nel grafico le linee tratteggiate indicano le posizioni della rete, le linee colorate quelle affinate, il grigio gli eventi esclusi. Il click WAV mantiene i canali originali e usa le posizioni video esportate, comprese le correzioni manuali, e mantiene la durata del file sorgente; con `--source-in` i click restano nelle coordinate originali del file.

Per importare in Resolve lo stesso PCM usato come origine temporale dell’analisi, aggiungere `--pcm /output/music-pcm.wav` e utilizzare quel WAV nella timeline. La copia mantiene i canali originali, viene ricampionata a 44.1 kHz ed è salvata a 32 bit float. Può aiutare a distinguere eventuali differenze di decodifica MP3 dagli errori del rilevatore.

Il JSON schema 2 contiene `beats` (tutti gli eventi analizzati), `bars` (gli inizi di misura con durata) e `markers` (gli eventi esportati). Ogni evento conserva tempi originali e affinati, segnali di qualità e `export_status`. L'ultima misura viene segnalata come potenzialmente incompleta. Le durate si riferiscono al file sorgente, non alla clip rifilata. I campi timecode sono presenti sugli eventi effettivamente esportati.

## Misurare il miglioramento

Preparare un file TXT con una posizione annotata manualmente in **secondi del sorgente** per riga; sono ammessi commenti dopo `#`. In alternativa usare una lista JSON di numeri. I riferimenti devono indicare lo stesso tipo di evento scelto: tutti i tempi o solo inizi di misura.

```bash
docker run --rm -v "$(pwd):/workspace:ro" --workdir /workspace \
  --entrypoint python music-bar-detector \
  -m app.evaluate output/music.json input/reference-downbeats.txt \
  --kind downbeats --stage original --tolerance-ms 20

docker run --rm -v "$(pwd):/workspace:ro" --workdir /workspace \
  --entrypoint python music-bar-detector \
  -m app.evaluate output/music.json input/reference-downbeats.txt \
  --kind downbeats --stage refined --tolerance-ms 20
```

`--kind markers --stage exported` valuta invece i punti realmente esportati. In questo caso anche i riferimenti vengono limitati all'intervallo della clip. I marker esclusi da un filtro possono risultare tempi mancanti: è il compromesso tra precisione e copertura.

La valutazione abbina eventi uno a uno, massimizzando il numero di corrispondenze e poi minimizzando l'errore. Riporta precision, recall, F1, falsi positivi, tempi mancanti, errore medio, 95° percentile e anticipo/ritardo medio. Le statistiche temporali riguardano solo gli eventi abbinati: vanno lette insieme ai mancanti e ai falsi positivi. Usare più brani e anche tolleranze di 40 e 70 ms; 20 ms è una soglia severa per il montaggio a 25 fps.

## Test e installazione Python

```bash
docker run --rm -v "$(pwd):/workspace:ro" --workdir /workspace \
  --entrypoint python music-bar-detector -m unittest discover -s tests -v
```

I test coprono timecode frazionari/drop-frame, quantizzazione, trim/offset, duplicati, filtraggio, transienti sintetici, silenzi, stereo in opposizione di fase e metriche uno a uno. Non certificano l'accuratezza musicale su brani senza riferimenti umani.

Per un ambiente Python 3.11 separato con FFmpeg disponibile:

```bash
python -m pip install -r requirements.txt
python -m pip install 'git+https://github.com/imcmurray/madmom-modern.git@df718c704a3b460ac6c61b7e61663a0436b91bda'
# Facoltativo:
python -m pip install -r requirements-beat-this.txt
python -m app.detect_bars input/music.mp3 --output output/music.json --edl output/music.edl
```

Le dipendenze principali e la revisione di madmom sono fissate; le versioni effettive dei pacchetti vengono registrate nei risultati. Le dipendenze transitive e l'immagine base Docker non sono bloccate integralmente. `python -m app.detect_bars --help` mostra tutte le opzioni.
