# Starvation: applicazione, test e risultati

12 ottobre 2026. Dati: `experiments/results/starvation/` (campagna del 6 ottobre 2026). Grafici: `experiments/plots/starvation/`.

**In breve.** Su 180 punti (single core, 1–2048 code, 1–3 Mpps) nessuna policy affama le code fredde senza motivo: il service gap segue la durata del giro di polling. La vera starvation è quella delle code calde sotto baseline, che legge 32 pacchetti per coda a ogni giro. A 2048 code un flusso di 1024 pacchetti impiega 98 ms con baseline e 7 ms con Aggressive. Aggressive ha un costo, ma lo paga solo in sovraccarico: allunga il giro e quindi l'attesa delle code fredde (fino a 7–13 ms contro 1–12 ms di baseline), però non perde pacchetti e tratta tutti i flussi allo stesso modo.

## Indice

1. [La domanda](#la-domanda)
2. [L'applicazione](#lapplicazione-nf-chain-con-una-sonda-per-poll)
3. [Il traffico](#il-traffico-tre-tipi-di-stream-in-un-solo-profilo)
4. [La campagna](#la-campagna-180-punti-unapp-nuova-per-ogni-punto)
5. [Metriche](#metriche)
6. [Risultati](#risultati)
7. [I grafici, uno per uno](#i-grafici-uno-per-uno)
8. [Le policy una per una](#le-policy-una-per-una)
9. [Limiti e punti da verificare](#limiti-e-punti-da-verificare)

## La domanda

L'esperimento verifica se le policy lasciano qualche coda senza servizio (starvation) e quanto questo costa ai singoli flussi. La campagna NDR misura solo il throughput aggregato. Qui invece si guarda il singolo pacchetto e la singola coda.

Il rischio è concreto per due policy:

- **Aggressive** resta su una coda finché restituisce un burst pieno (32 pacchetti), fino a 100 poll consecutivi (`QFLOW_MAX_CONSECUTIVE_POLLS`). Nel frattempo le altre code aspettano.
- **Penalty** salta per 5 giri una coda (`QFLOW_DEFAULT_PENALTY`) quando il poll precedente ha restituito al più metà burst. Un flusso piccolo che arriva su una coda penalizzata aspetta che la penalità scada.

Le domande concrete sono tre:

1. Quanto può restare una coda con pacchetti senza essere visitata (service gap)?
2. Quanto ritardo in più vede un flusso a seconda del suo rango Zipf, cioè della sua popolarità?
3. Quanto si allunga il tempo di completamento (FCT) di un flusso finito di N pacchetti?

## L'applicazione: nf-chain con una sonda per poll

`starvation/` è nf-chain (ACL, Count-Min Sketch, NAT) identico all'originale riga per riga, più una sola chiamata `qp_poll()` subito dopo ogni `rte_eth_rx_burst()`. Il diff con `nf-chain/src/main.c` è di circa 20 righe: l'include di `src/qprobe.h`, le opzioni `--log FILE` e `--log-records N` (default 8M record), `qp_init`/`qp_start` prima del loop e `qp_finish` all'uscita. Le policy e il loop di polling sono quindi esattamente quelli misurati nella campagna NDR.

A ogni poll `qp_poll()` legge il TSC una volta e fa due cose.

**1. Due intervalli per coda**, aggiornati solo sui poll che restituiscono pacchetti e solo dopo 3 s di warmup:

- **Drain gap** = adesso − ultimo poll che ha lasciato la coda vuota (cioè con nb_rx < 32). È un limite superiore al tempo che il pacchetto più vecchio del burst ha passato nell'anello RX. Su una coda che non si svuota mai (sovraccarico, o coda calda sotto Aggressive) cresce con il periodo di attività.
- **Service gap** = adesso − poll precedente della stessa coda. È il tempo in cui la coda aveva pacchetti e nessuno l'ha visitata: l'intervallo di starvation vero e proprio. Vale sempre service gap ≤ drain gap.

Per ogni coda si tengono massimo di entrambi, pacchetti e poll non vuoti. Due istogrammi log2(cicli), pesati per pacchetti, danno le distribuzioni globali. I salti di Penalty non fanno un poll e quindi non spostano nessuno dei due timestamp.

**2. Un log dei pacchetti marcati.** Il profilo TRex scrive un marcatore nel payload UDP all'offset 42:

| Marcatore | Contenuto | Quando si registra |
| --- | --- | --- |
| `QB` | flow_id, seq del field engine, shift | flusso Zipf campionato, solo se seq % 2^shift = 0 |
| `QL` | flow_id + trailer di latenza TRex (16 B: magic 0xAB, seq, timestamp TX) | sonda o flusso finito, sempre |

Ogni record (TSC di ricezione, timestamp TRex, flow_id, seq, coda, posizione nel burst, dimensione del burst) finisce in un buffer per lcore allocato con `mmap`. All'uscita il buffer viene scritto in un file binario (header `QPROBE1`). L'app stampa anche le righe `qdrain q=…` e `qhist`/`qsvc` che lo script legge con le regex. Se il trailer TRex non si trova, il record è marcato `LAT_BAD`, per distinguerlo da un pacchetto perso.

## Il traffico: tre tipi di stream in un solo profilo

`experiments/starvation-profile.py` costruisce tre tipi di stream, tutti con la dimensione di pacchetto di `zipf-profile.py` (64 B di default).

| Stream | Cosa sono | A cosa servono |
| --- | --- | --- |
| Carico Zipf | Gli stessi 10.000 flussi e pesi di `zipf-profile.py` (stesso seed, stesse 5-tuple). 200 ranghi spaziati in log sono "campionati" (`QB`). | Generano il carico. I campionati danno perdite e riordino per flusso. |
| Sonde | Stream di latenza TRex a 500 pps con la 5-tupla dei ranghi 1, 3, 10, 30, 100, 300, 1000, 3000, 10000 (`QL`) | Finiscono nella stessa coda del flusso Zipf di quel rango e ne misurano il ritardo one-way. |
| Flussi finiti | Stream di latenza TRex con 5-tuple nuove (100.64.x.x), burst di N = 1, 4, 16, 64, 256, 1024 pacchetti a 200 kpps, 4 flussi per taglia | Misurano l'FCT di un flusso corto, medio o lungo che arriva in mezzo al carico. |

Alcuni dettagli contano per l'analisi:

- **Seq dei flussi campionati.** Il seq è una variabile del field engine (`inc`). Lo shift è scelto in modo che ogni flusso campionato registri circa 500 pacchetti/s al rate più alto: un flusso caldo si registra 1 volta su 2^shift, uno freddo sempre. Così il log resta di dimensione fissa.
- **Interleaving sui core TRex.** TRex divide una variabile del field engine sui suoi 4 core DP alternandoli: il core j invia j, j+4, j+8… con la propria temporizzazione. Perdite e riordino vanno quindi calcolati per sottoflusso `seq % 4`, altrimenti lo sfasamento tra core sembra riordino.
- **Rate.** TRex non scala gli stream di latenza con il moltiplicatore della porta: scala solo il carico Zipf, la cui base è 1 pps per flusso. Quindi `mult = rate / 10000`. Sonde (9 × 500 = 4,5 kpps) e flussi finiti (≈ 77 kpps in media, vedi sotto) si aggiungono al rate nominale.
- **Periodo dei flussi finiti.** Ogni flusso finito invia in media min(5000, 200 × N) pacchetti/s: al massimo 200 burst/s, e al massimo 5000 pacchetti/s. Con 4 flussi per taglia fa circa 77 kpps in tutto. Il primo burst parte a un offset casuale (seed 7), così i flussi non sono in fase.

## La campagna: 180 punti, un'app nuova per ogni punto

`experiments/starvation.py` ha girato con i default il 6 ottobre 2026: 180 punti in 3447 s (circa 57 min), nessun punto fallito. Il risultato è in `experiments/results/starvation/`, il log in `logs/starvation_campaign.log`.

| Parametro | Valori |
| --- | --- |
| Skew Zipf | 0,1 · 0,6 · 0,9 |
| Code RX | 1 · 64 · 128 · 1024 · 2048 |
| Policy | baseline · aggressive · penalty · aggressive+penalty |
| Rate Zipf offerto | 1 · 2 · 3 Mpps (rate fisso, non NDR) |
| Core | 1 (lcore 0, `--config=(0,0,0)`) |
| Flussi | 10.000 |
| Warmup / misura | 3,5 s / 8 s |
| Ripetizioni | 1 per punto |

Per ogni punto lo script fa cinque passi:

1. Avvia `starvation/build/starvation -q Q --policy P --log …bin` e aspetta `ready`.
2. Avvia TRex al rate scelto, aspetta 3,5 s (l'app fa 3 s di warmup), azzera le statistiche e misura per 8 s.
3. Legge da TRex i contatori di porta e, per ogni pg_id, latenza round-trip (min, media, p99, max) e pacchetti TX/RX.
4. Ferma TRex e poi l'app con SIGTERM. L'app scrive il log binario e stampa le righe `qdrain`/`qhist`/`qsvc`.
5. Analizza il log, scrive le righe nei CSV e cancella il `.bin` (salvo `--keep-logs`).

Il rate è fisso perché qui interessa il comportamento sotto un carico dato, non il massimo senza perdite. A 3 Mpps molte configurazioni sono in sovraccarico: con skew 0,1 e 0,6 perdono già con 1 coda (1,4–8,5%), mentre con skew 0,9 una coda regge senza perdite grazie alla località.

Il profilo si ricarica solo quando cambia lo skew. Gli shift dei flussi campionati sono calcolati sul rate massimo (3 Mpps), quindi a 1 Mpps ogni flusso registra circa 1/3 dei campioni.

## Metriche

L'analisi (`analyze()` in `starvation.py`) trasforma il log in quattro famiglie di metriche.

**Ritardo one-way delle sonde e dei flussi finiti.** Il timestamp TX di TRex e il TSC dell'host sono due orologi diversi. Lo script fa un fit lineare "dal basso" (`lower_fit_line`): divide i punti (trex_ts, tsc) in 40 finestre, prende il minimo di ogni finestra e ripete il fit 3 volte. Il ritardo è TSC − retta, quindi è relativo al ritardo minimo, non assoluto. Come controllo indipendente si tiene anche la latenza round-trip che TRex misura per pg_id.

**FCT e slowdown.** I pacchetti di un flusso finito si separano in burst dove il TX ha un buco di almeno max(20 µs, 10 periodi). Il primo e l'ultimo burst si scartano. Per ogni burst di N pacchetti:

- FCT = ricezione dell'ultimo pacchetto − TX del primo.
- Slowdown = (FCT + ritardo base) / (durata TX + ritardo base). Il ritardo base è metà del minimo RTT TRex, circa 2–3 µs.

La durata TX ideale va da 0 (N = 1) a 5,1 ms (N = 1024 a 200 kpps).

**Perdite e riordino.** Per i flussi campionati il seq si analizza per sottoflusso `seq % 4`, con passo lcm(4, 2^shift). Per sonde e flussi finiti si usa il seq del trailer TRex. Si contano perdite, riordini (un seq più basso del precedente) e duplicati.

**Gap per coda.** Dalle righe `qdrain` lo script ricava per ogni coda il carico (pacchetti/s), il service gap massimo e il drain gap massimo. Ricava anche il tempo di overflow dell'anello, 1024 descrittori / carico: se il drain gap lo supera, la coda può aver perso pacchetti. Dagli istogrammi log2 ricava p99 e p99,9 globali.

Due cautele di lettura. I percentili da istogramma sono quantizzati a potenze di 2: i valori ricorrenti 15, 30, 60, 120, 239, 479, 958, 1916, 3832, 7664 e 15327 µs sono centri di bucket, non misure esatte. E sotto sovraccarico il drain gap vale circa 11,5 s, cioè tutta la misura, perché la coda non si svuota mai. In quel caso conta il service gap.

## Risultati

La starvation che conta non è quella delle code fredde sotto Aggressive. È quella delle code calde sotto baseline: baseline legge al massimo 32 pacchetti per coda a ogni giro, e con molte code il giro dura millisecondi. Aggressive e Penalty allungano un po' l'attesa delle code fredde, ma solo quando il sistema è già in sovraccarico, e in cambio Aggressive non perde pacchetti.

![FCT mediano di un flusso da 1024 pacchetti contro il numero di code](plots/starvation/fct1024_vs_queues_s0.6_r2.png)

*FCT mediano di un flusso da 1024 pacchetti a 200 kpps, skew 0,6, carico Zipf 2 Mpps. Fonte: `results/starvation/fct.csv`, 4 flussi per punto, circa un burst ogni 5 ms per 8 s.*

Il conto torna con il giro di polling. A 2048 code e 2 Mpps una coda viene visitata con pacchetti circa 320–360 volte al secondo, cioè ogni ~3 ms. Baseline svuota 32 pacchetti per visita, quindi 1024 pacchetti richiedono 32 visite: 32 × 3 ms ≈ 96 ms, contro i 98 ms misurati. Aggressive resta sulla coda finché il burst è pieno (fino a 100 poll) e chiude il flusso in 7,4 ms. Penalty accorcia il giro saltando le code vuote, ma non svuota la coda calda: 22,5 ms. Con 1–128 code tutte le policy stanno entro circa il 5% dell'ideale, tranne baseline a 128 code (8,5 ms). Lo stesso andamento si vede nel p99 e nei flussi da 256 pacchetti: a 2048 code baseline 27 ms, aggressive 5,1 ms, ideale 1,3 ms.

### 1. Il service gap cresce con il numero di code, per tutte le policy

Il service gap misura il giro di polling, e il giro si allunga con le code. Fino al punto di saturazione le quattro policy sono quasi equivalenti.

| Code | Service gap max a 1 Mpps (µs) | Service gap max a 2 Mpps (µs) |
| --- | --- | --- |
| 1 | 80–170 | 90–175 |
| 64 | 130–225 | 225–1250 |
| 128 | 140–320 | 310–1210 |
| 1024 | 230–930 | 1400–3600 |
| 2048 | 540–1480 | 2000–6100 |

Gli intervalli coprono le 4 policy e i 3 skew. Con 1 coda, a 1–2 Mpps, il p99,9 è sempre il bucket da 7–15 µs. Il massimo di 100–170 µs è un evento singolo per run.

### 2. Sotto sovraccarico Aggressive allunga l'attesa delle code fredde, ma non perde

A 3 Mpps Aggressive e Penalty alzano il service gap massimo rispetto a baseline. In cambio Aggressive non ha code oltre il tempo di overflow dell'anello (1024 descrittori / carico) e perde ≤ 0,05% in tutti i punti con 64–2048 code e skew 0,1–0,6.

| 3 Mpps, skew 0,1 | Service gap max (ms) | Perdita | Code in overflow |
| --- | --- | --- | --- |
| 64 code, baseline | 1,1 | 12,6% | 63 |
| 64 code, aggressive | 7,2 | 0,01% | 0 |
| 64 code, penalty | 4,6 | 12,6% | 64 |
| 128 code, baseline | 2,0 | 12,6% | 107 |
| 128 code, aggressive | 5,2 | 0,01% | 0 |
| 128 code, penalty | 10,3 | 7,5% | 126 |
| 2048 code, baseline | 12,2 | 1,6% | 52 |
| 2048 code, aggressive | 12,7 | 0,02% | 0 |

Il gap massimo più alto della campagna è 29,5 ms (aggressive+penalty, 1024 code, skew 0,6, 3 Mpps), senza perdite (0,05%). Con skew 0,9 a 1024–2048 code e 3 Mpps perdono tutte le policy: aggressive 2,6–4,5%, baseline 18–22%.

### 3. Sotto baseline il ritardo dipende dalla coda in cui cade il flusso

Le sonde misurano il ritardo dei flussi Zipf di rango 1–10.000. Con Aggressive il p99 è piatto su tutti i ranghi. Con baseline la coda dei ranghi più popolari diventa un collo di bottiglia.

| Configurazione | Rango 1 (µs, p99) | Rango 10.000 (µs, p99) |
| --- | --- | --- |
| 2048 code, 2 Mpps, skew 0,6, baseline | 100.605 | 5.942 |
| 2048 code, 2 Mpps, skew 0,6, aggressive | 6.345 | 6.524 |
| 2048 code, 3 Mpps, skew 0,9, baseline | 128.446 | 7.780 |
| 2048 code, 3 Mpps, skew 0,9, aggressive | 13.109 | 13.301 |

A 3 Mpps, con 64–128 code e skew 0,1–0,6, baseline e penalty sono in sovraccarico, e quasi tutte le sonde vedono 22–47 ms al p99. Aggressive resta a 3–5 ms.

### 4. Nessun riordino dovuto alle policy

Su 144 punti con 64–2048 code ci sono in tutto 4 pacchetti riordinati e 5 duplicati. Il riordino compare solo con 1 coda (8162 casi, tutti su flussi finiti) e quindi non dipende dalle policy: vedi i limiti. Il log non ha mai perso record (`log_dropped` = 0) e tutti i trailer TRex sono stati trovati (`lat_bad` = 0).

## I grafici, uno per uno

Tutti i grafici usano gli stessi colori: baseline blu (linea continua), aggressive arancio (tratteggio), penalty verde (puntini), aggressive+penalty giallo (tratto-punto). Un punto vuoto o una curva sbiadita indica una perdita > 0,1%, cioè una configurazione in sovraccarico. Per quei punti il ritardo misura la coda piena, non la policy.

### CCDF del ritardo one-way (`delay_ccdf_probe_r*`, `delay_ccdf_fct_r*`)

**Come si legge.** Ogni pannello è una combinazione skew × numero di code, e c'è un file per rate (r1, r2, r3 = 1, 2, 3 Mpps). L'asse x è il ritardo one-way meno il minimo del run, in scala log. L'asse y è P(ritardo ≥ x), cioè la frazione di pacchetti che ha aspettato almeno x, anch'essa in scala log. Si legge da sinistra a destra:

- dove la curva **scende in verticale** sta il ritardo tipico: quasi tutti i pacchetti aspettano lo stesso tempo, e quel tempo è il giro di polling;
- a y = 10⁻² si legge il p99, a y = 10⁻³ il p99,9;
- un **gradino orizzontale** (plateau) vuol dire che una frazione fissa di pacchetti ha un ritardo molto più alto degli altri. L'altezza del gradino dice quanti sono.

Le sonde (`probe`) sono 9 stream da 500 pps, circa 40.000 pacchetti per run, quindi la curva arriva fino a circa 3·10⁻⁵. Le `fct` sono i pacchetti dei flussi finiti, dominati dai flussi lunghi da 256 e 1024 pacchetti.

![CCDF del ritardo delle sonde a 2 Mpps](plots/starvation/delay_ccdf_probe_r2.png)

**Sonde, 2 Mpps (sotto la capacità).** Andando da sinistra a destra il gradino verticale si sposta da ~30 µs (1 coda) a ~150 µs (64 code), ~400 µs (128), ~3 ms (1024) e ~6 ms (2048): è il giro di polling che si allunga. Le quattro policy sono sovrapposte, quindi nessuna aggiunge ritardo ai flussi normali. L'unica differenza è baseline (blu sbiadito) a 1024–2048 code con skew 0,6 e 0,9: ha un plateau a P ≈ 0,1–0,2 che si allunga fino a 30–100 ms. Le 9 sonde valgono ciascuna 1/9 ≈ 11% dei pacchetti, quindi il plateau corrisponde a una o due sonde: proprio quelle dei ranghi 1 e 3, che stanno sulle code più calde. È la starvation delle code calde vista dal lato del pacchetto. Le altre policy non hanno il plateau.

![CCDF del ritardo delle sonde a 3 Mpps](plots/starvation/delay_ccdf_probe_r3.png)

**Sonde, 3 Mpps (sovraccarico).** Qui si vede il compromesso di Aggressive.

- A 64–128 code con skew 0,1–0,6 baseline e penalty (sbiaditi) restano a P ≈ 1 fino a 20–45 ms: quasi tutti i pacchetti aspettano un anello pieno. Aggressive scende a 3–5 ms e aggressive+penalty poco dopo.
- A 1024–2048 code con skew 0,1–0,6 il corpo della curva di baseline sta invece **a sinistra** di quella di Aggressive: il ritardo mediano è più basso (per esempio 3,4 ms contro 11,3 ms a 1024 code e skew 0,1). È il costo di Aggressive: a ogni giro resta fino a 100 poll sulle code piene, il giro si allunga, e tutte le code fredde aspettano di più. Baseline però ha una coda lunga: un plateau a 10⁻³ che arriva a 100–260 ms, e perde l'1,5–7%. Aggressive tiene invece tutti i pacchetti entro 20–25 ms e non perde.
- Con skew 0,9 a 64–128 code baseline ha di nuovo il plateau delle code calde (~10%, fino a 15–18 ms), mentre Aggressive scende pulito a 2–4 ms.

![CCDF del ritardo dei flussi finiti a 2 Mpps](plots/starvation/delay_ccdf_fct_r2.png)

**Flussi finiti, 2 Mpps.** Qui il problema di baseline si vede già da 128 code e anche con skew 0,1. Un flusso finito arriva a 200 kpps: anche se l'anello è freddo, il flusso stesso rende la coda calda per la durata del burst. Baseline ne legge 32 per giro, quindi i pacchetti successivi si accodano. La curva blu ha una spalla lunga che arriva a 3–7 ms (128 code) e a 20–50 ms (1024–2048 code). Aggressive e aggressive+penalty scendono in verticale al giro di polling (~5 ms a 2048 code). Penalty sta in mezzo, perché accorcia il giro ma non svuota la coda.

### Service gap (`service_gap_max`, `service_gap_p999`)

![Service gap massimo](plots/starvation/service_gap_max.png)

Una riga per skew e una colonna per rate. A 1–2 Mpps le quattro linee sono sovrapposte e crescono con il numero di code: da ~100 µs con 1 coda a 1–6 ms con 2048. È la durata del giro, uguale per tutte le policy. A 3 Mpps le linee si separano: baseline (blu, punti vuoti perché perde) ha il gap più basso, Aggressive e Penalty più alto, fino a 10–30 ms. La lettura giusta non è "Aggressive affama le code fredde". In sovraccarico baseline ha un gap basso perché a ogni visita legge al massimo 32 pacchetti e scappa via, lasciando dietro un anello pieno che perde. Aggressive spende il tempo a svuotare. `service_gap_p999` ha lo stesso andamento, quantizzato a potenze di 2.

### Service gap per coda contro carico (`queue_scatter_s*_r*`)

![Service gap per coda a skew 0,6, 3 Mpps](plots/starvation/queue_scatter_s0.6_r3.png)

Ogni punto è una coda: in x il suo carico (pacchetti/s), in y il suo service gap massimo. La diagonale punteggiata è il tempo di overflow dell'anello, 1024 / carico. Sopra la diagonale una coda non visitata per quel tempo perde pacchetti. Due cose si vedono:

- le nuvole sono **orizzontali**: il service gap massimo non dipende dal carico della coda, tutte le code aspettano lo stesso giro. Nessuna policy sceglie di trascurare le code poco cariche;
- i punti di baseline e penalty stanno **sotto** la diagonale, eppure quelle configurazioni perdono. La diagonale assume che una visita svuoti la coda, e baseline non lo fa: legge 32 pacchetti e il resto si accumula giro dopo giro. Per baseline il tempo giusto è il drain gap, che in sovraccarico vale tutta la misura. Aggressive svuota a ogni visita, quindi per lui la diagonale è il criterio corretto, e infatti non perde.

### FCT e slowdown (`fct_s*`, `fct_slowdown_s*`)

![Slowdown FCT a skew 0,6](plots/starvation/fct_slowdown_s0.6.png)

Una riga per rate e una colonna per numero di code. In x la dimensione del flusso, in y lo slowdown (FCT / durata ideale), linee piene p99 e puntinate p50. Lo slowdown dei flussi da 1 pacchetto è alto (fino a 1000–3000 con 2048 code) per tutte le policy: un flusso da un pacchetto ha durata ideale zero, quindi lo slowdown è solo il ritardo di un giro diviso pochi µs. Non è un effetto della policy. Per i flussi lunghi (64–1024 pacchetti) lo slowdown di Aggressive scende verso 1–4, mentre baseline resta a 10–20 da 1024 code in su a 2 Mpps. È lo stesso effetto del grafico FCT in testa ai risultati. `fct_s*` mostra gli stessi dati in microsecondi.

### Ritardo per rango Zipf (`rank_delay_s*_r*`)

![Ritardo delle sonde per rango Zipf, skew 0,6, 2 Mpps](plots/starvation/rank_delay_s0.6_r2.png)

In x il rango del flusso Zipf su cui sta la sonda (1 = il più popolare), in y il ritardo massimo (linea piena) e il p99 (puntini). Linee piatte vogliono dire che tutti i flussi sono trattati allo stesso modo. Sono piatte per tutte le policy fino a 128 code. A 1024 e 2048 code baseline ha un salto a 60–100 ms sui ranghi 1 e 3: sono i flussi più popolari, le cui code sono abbastanza calde da non svuotarsi a 32 pacchetti per giro. Aggressive resta piatto. Nel pannello da 64 code Aggressive ha un massimo un po' più alto degli altri (1,7 ms contro 0,3–0,6 ms): è un singolo episodio in cui il giro si è allungato per svuotare una coda calda.

### Perdita (`loss`)

![Perdita contro numero di code](plots/starvation/loss.png)

Perdita TRex in scala log. A 1–2 Mpps quasi tutto è sotto lo 0,01%. Le eccezioni sono baseline da 1024 code in su con skew 0,6–0,9 (0,16–11%) e penalty a 2048 code con skew 0,9 (1,3%). A 3 Mpps baseline e penalty perdono il 2–12%, Aggressive e aggressive+penalty restano sotto lo 0,05% fino a 2048 code con skew 0,1–0,6. Il crollo a 64 code delle curve arancio e gialla è il guadagno di Aggressive misurato nella campagna NDR, visto qui come perdita.

## Le policy una per una

### Baseline (round-robin, 32 pacchetti per visita)

Visita ogni coda una volta per giro e legge al più un burst. È equa per coda, non per pacchetto: una coda con 1 pacchetto e una con 1000 ricevono lo stesso servizio. Con poche code va bene, perché il giro è breve (~7 µs con 1 coda, ~60–100 µs con 64). Con 1024–2048 code il giro dura 3–6 ms, e una coda che riceve più di 32 pacchetti per giro non si svuota più: a 2048 code e 2 Mpps sono le code sopra ~10 kpps. Lì nascono il plateau delle CCDF, il salto per rango e l'FCT di 98 ms. In sovraccarico baseline ha il service gap massimo più basso di tutte, ma solo perché non svuota nulla: perde fino al 12,6% e ha fino a 107 code oltre il tempo di overflow.

### Aggressive (resta finché il burst è pieno, fino a 100 poll)

**Cosa fa.** Dopo un poll che restituisce 32 pacchetti, Aggressive rilegge la stessa coda invece di passare alla successiva. Si ferma al primo burst parziale o dopo 100 poll consecutivi, cioè al massimo 3200 pacchetti per visita. Con una coda poco carica il primo poll è già parziale e il comportamento è identico a baseline.

**Quando cambia qualcosa.** Solo sulle code che a ogni visita trovano almeno un burst pieno, cioè le code calde o in sovraccarico. Sotto la capacità, con 1–128 code, quasi nessuna coda ha un burst pieno, e in tutti i grafici Aggressive è sovrapposto a baseline.

**Il beneficio.** Svuota le code calde a ogni visita, quindi:

- con 64–2048 code nessuna coda supera il tempo di overflow dell'anello, in nessun punto della campagna (contro fino a 107 code di baseline a 3 Mpps);
- perdita ≤ 0,05% a 3 Mpps con 64–2048 code e skew 0,1–0,6, contro l'1,6–12,6% di baseline;
- un flusso lungo finisce vicino al tempo ideale: 7,4 ms contro 5,1 ms ideali a 2048 code, contro 98 ms di baseline;
- il ritardo non dipende dalla popolarità del flusso (`rank_delay` piatto, p99 di 6,3 ms sul rango 1 e 6,5 ms sul rango 10.000).

**Il costo.** Il tempo speso a svuotare una coda calda è tempo in cui le altre aspettano. Si vede in tre posti:

- il service gap massimo in sovraccarico: 7,2 ms contro 1,1 ms di baseline a 64 code (3 Mpps, skew 0,1), 5,2 contro 2,0 ms a 128 code;
- il corpo della CCDF a 3 Mpps con 1024–2048 code: il ritardo mediano delle sonde è più alto che con baseline (11,3 contro 3,4 ms a 1024 code e skew 0,1; 13,0 contro 4,4 ms a 2048 code e skew 0,6);
- qualche picco isolato sotto la capacità, come il massimo di 1,7 ms a 64 code e 2 Mpps contro 0,3–0,6 ms delle altre policy.

**Quanto può costare al massimo.** Ogni visita a una coda calda dura al più 100 poll × 32 pacchetti. Con nf-chain a circa 0,3 µs per pacchetto sono circa 1 ms per coda calda per giro (stima, non misurata). Il limite di 100 poll serve proprio a questo: senza, una coda sempre piena terrebbe il core per sempre. Il gap massimo osservato con Aggressive da solo è 12,8 ms (2048 code, 3 Mpps, skew 0,6).

**In sintesi.** Aggressive sposta l'attesa dalle code calde a quelle fredde, e solo quando c'è sovraccarico. In cambio non perde pacchetti e tratta tutti i flussi allo stesso modo. Baseline fa il contrario: a ogni coda dà lo stesso tempo, e chi ha più traffico perde pacchetti o aspetta decine di millisecondi.

### Penalty (salta per 5 giri una coda poco produttiva)

Dopo un poll con al più 16 pacchetti, la coda viene saltata per i 5 giri successivi. Con molte code quasi vuote accorcia il giro, ed è il suo scopo: a 1024–2048 code, skew 0,1 e 3 Mpps porta la perdita dall'1,5% di baseline allo 0,03%. Però non svuota le code calde. A 64–128 code e 3 Mpps perde come baseline (7,5–12,6%), e l'FCT di un flusso da 1024 pacchetti a 2048 code è 22,5 ms, a metà tra baseline e Aggressive. Il salto costa qualcosa anche ai flussi piccoli che arrivano su una coda penalizzata: a 64 code e 2 Mpps con skew 0,1 il service gap massimo di Penalty è 1,25 ms contro 0,23–0,30 ms delle altre, e nella CCDF delle sonde di quel pannello Penalty ha la coda verde più lunga.

### Aggressive+penalty

Si comporta come Aggressive per le code calde e come Penalty per quelle fredde. Ha le stesse perdite basse di Aggressive e un FCT dei flussi lunghi simile (6,9 ms a 2048 code). Somma però le due attese: è la configurazione con il service gap massimo più alto della campagna (29,5 ms a 1024 code, skew 0,6, 3 Mpps, senza perdite), e a 128 code e 3 Mpps il suo ritardo delle sonde è più alto di quello di Aggressive da solo (p99 8,3–9,4 ms contro 5,1–5,2 ms).

## Limiti e punti da verificare

I risultati qualitativi sono solidi: sono coerenti tra skew e rate, e il conto 32 × giro spiega l'FCT di baseline. Prima di citare numeri esatti restano però questi punti.

- **Una ripetizione per punto.** Non ci sono barre d'errore. I massimi (service gap max, ritardo max) sono eventi singoli e possono variare molto tra run.
- **Ritardo prima del poll.** A 2 Mpps il ritardo mediano delle sonde è circa 1,6 volte l'intervallo tra due poll non vuoti della loro coda, con 64–2048 code. A 2048 code (aggressive, skew 0,6) vale 4,8 ms, mentre il service gap massimo della stessa coda è 4,5–4,9 ms. Anche l'RTT misurato da TRex dà 4,8 ms in media, quindi non è un errore del fit degli orologi. Una parte dell'attesa avviene prima che il pacchetto sia visibile a `rte_eth_rx_burst()`: nella QDMA (writeback delle completion, prefetch dei descrittori) o nel PMD. Va capito prima di attribuire tutto il ritardo al giro di polling.
- **Riordino e duplicati con 1 coda.** Compaiono solo con 1 coda e solo sui flussi finiti (stream TRex multi-burst). Una sola coda FIFO non può riordinare, quindi l'origine è probabilmente TRex o l'analisi del seq del trailer. Non tocca le policy, ma va chiarito prima di usare questi dati per parlare di riordino.
- **Un solo core.** La campagna non copre Shared e Partitioned.
- **Rate fisso.** I punti a 3 Mpps sono in gran parte in sovraccarico. Per un confronto "a parità di perdita zero" servirebbe ripetere al rate NDR di ogni configurazione.
- **Percentili quantizzati.** I p99 e p99,9 dei gap vengono da istogrammi log2: distinguono solo fattori 2.
- **Ritardo relativo.** Il one-way è relativo al minimo del run (base ≈ 2 µs), non assoluto.

Le prove di sviluppo `results_starvation_smoke*` (1–6 punti) sono state cancellate.

Per rigenerare i grafici: `cd experiments && python3 plot_starvation.py`. Il grafico FCT in testa ai risultati (`fct1024_vs_queues_s0.6_r2.png`) non viene da `plot_starvation.py`: è stato generato a parte da `fct.csv`.
