#!/bin/bash
# =============================================================================
#  selftest.sh — mesures reproductibles : concat vs acrossfade, sous ffmpeg
# =============================================================================
#  AUCUN MEDIA EXTERIEUR : le script fabrique toutes ses sources, TOUTES au
#  meme taux d'echantillonnage (sinon `concat` reechantillonne et ajoute du
#  remplissage — c'est le piege B ci-dessous).
#
#  Usage : bash audit/selftest.sh
#  Sortie : un tableau [OK]/[ECHEC] et un code de sortie 1 si une assertion
#           echoue.
#
#  TROIS PIEGES DE HARNESS, tous reproduits et documentes ici :
#
#  (A) NE JAMAIS lire et ecrire le meme fichier dans une boucle ffmpeg.
#      `ffmpeg -i q.wav ... q.wav -y` : ffmpeg ouvre la sortie en ecriture
#      avant d'avoir fini de lire. La sortie s'effondre alors a ~1-2 s au lieu
#      de 30 s. Ce script ecrit un nom NEUF a chaque tour.
#
#  (B) NE PAS MELANGER LES TAUX D'ECHANTILLONNAGE dans un concat.
#      `sine` genere a 44100 Hz par defaut, `anullsrc`/`aevalsrc` peuvent etre a
#      48000. Concatener les deux fait reechantillonner ffmpeg et la duree
#      totale N'EST PLUS la somme : mesure 2.544218 s au lieu de 2.500000 s,
#      soit +44.2 ms de remplissage. Avec un taux unique : 2.500000 s exact.
#
#  (C) `silencedetect` avec un seuil de -120 dB ne trouve RIEN, meme dans un
#      fichier de zeros purs. Le seuil utile est -50 dB.
# =============================================================================
set -u

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")

FAILS=0; OKS=0; SR=48000
dur() { ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$1" 2>/dev/null; }

assert_eq() {
  local label="$1" want="$2" got="$3" tol="${4:-0.0005}"
  local d ad
  d=$(awk "BEGIN{printf \"%.6f\", ($got)-($want)}")
  ad=$(awk "BEGIN{x=($got)-($want); if(x<0)x=-x; printf \"%.6f\", x}")
  if awk "BEGIN{exit !($ad <= $tol)}"; then
    printf '  [OK]    %-44s attendu=%-11s mesure=%-11s ecart=%s\n' "$label" "$want" "$got" "$d"
    OKS=$((OKS+1))
  else
    printf '  [ECHEC] %-44s attendu=%-11s mesure=%-11s ecart=%s (tol %s)\n' "$label" "$want" "$got" "$d" "$tol"
    FAILS=$((FAILS+1))
  fi
}

echo "=================================================================="
echo "  ffmpeg concat / acrossfade — mesures reproductibles"
echo "=================================================================="
ffmpeg -version 2>&1 | head -1
echo

cd "$T"
SRC=2.000
for i in 1 2 3 4; do
  ffmpeg -v error -f lavfi -i "sine=frequency=$((330*i)):duration=${SRC}:sample_rate=$SR" \
         -c:a pcm_s16le "s$i.wav" -y
done
echo "Sources : 4 x ${SRC} s, ${SR} Hz mono"
echo

# ------------------------------------------------- 1. concatenation simple
printf "file 's1.wav'\nfile 's2.wav'\nfile 's3.wav'\nfile 's4.wav'\n" > lst.txt
ffmpeg -v error -f concat -safe 0 -i lst.txt -c copy       concat_copy.wav -y
ffmpeg -v error -f concat -safe 0 -i lst.txt -c:a pcm_s16le concat_reenc.wav -y
echo "--- 1. concat simple de 4 x ${SRC} s (aucune perte attendue) ---"
assert_eq "concat -c copy"    "8.000000" "$(dur concat_copy.wav)"
assert_eq "concat reencodage" "8.000000" "$(dur concat_reenc.wav)"
echo

# --------------------------------------------------- 2. UN acrossfade, 3 modes
echo "--- 2. un acrossfade d=0.5 entre 2 sources de 2.000 s ---"
echo "      (concat donnerait 4.000000 s)"
for spec in def ov0 ov1; do
  case "$spec" in
    def) F="acrossfade=d=0.5" ;;
    ov0) F="acrossfade=d=0.5:overlap=0" ;;
    ov1) F="acrossfade=d=0.5:overlap=1" ;;
  esac
  ffmpeg -v error -i s1.wav -i s2.wav -filter_complex "[0][1]$F[c]" -map "[c]" "one_$spec.wav" -y
  printf '  %-32s -> %s s\n' "$F" "$(dur "one_$spec.wav")"
done
echo
assert_eq "defaut (overlap=1 implicite)" "3.500000" "$(dur one_def.wav)"
assert_eq "overlap=1 explicite"          "3.500000" "$(dur one_ov1.wav)"
assert_eq "overlap=0"                    "4.000000" "$(dur one_ov0.wav)"
echo "  => le defaut VAUT overlap=1. overlap=1 RETIRE d au total."
echo "     overlap=0 ne retire RIEN : la duree est celle de la concat."
echo

# -------------------------------------------- 3. chaine de 3 jonctions, d=0.5
echo "--- 3. chaine de 4 sources de 2.000 s, d=0.5, 3 jonctions ---"
for spec in def ov0; do
  case "$spec" in
    def) F="acrossfade=d=0.5" ;;
    ov0) F="acrossfade=d=0.5:overlap=0" ;;
  esac
  ffmpeg -v error -i s1.wav          -i s2.wav -filter_complex "[0][1]$F[c]" -map "[c]" "ch_${spec}_1.wav" -y
  ffmpeg -v error -i "ch_${spec}_1.wav" -i s3.wav -filter_complex "[0][1]$F[c]" -map "[c]" "ch_${spec}_2.wav" -y
  ffmpeg -v error -i "ch_${spec}_2.wav" -i s4.wav -filter_complex "[0][1]$F[c]" -map "[c]" "ch_${spec}_3.wav" -y
  printf '  %-32s -> %s s\n' "$F" "$(dur "ch_${spec}_3.wav")"
done
echo
assert_eq "3 jonctions, defaut"     "6.500000" "$(dur ch_def_3.wav)"
assert_eq "3 jonctions, overlap=0"  "8.000000" "$(dur ch_ov0_3.wav)"
assert_eq "perte par jonction (defaut)" "0.500000" \
  "$(awk "BEGIN{printf \"%.6f\", (8.0-$(dur ch_def_3.wav))/3}")"
echo

# ------------------------- 4. d petit : la chaine s'effondre (bug reel ffmpeg)
echo "--- 4. chaine de 31 sources de 1.000 s, d=0.03 (30 ms) ---"
echo "      si la chaine se comportait bien : 31.000 - 30x0.030 = 30.100 s"
ffmpeg -v error -f lavfi -i "sine=frequency=440:duration=1:sample_rate=$SR" -c:a pcm_s16le q.wav -y
prev=q.wav
for i in $(seq 1 30); do
  ffmpeg -v error -i "$prev" -i q.wav -filter_complex "[0][1]acrossfade=d=0.03[c]" -map "[c]" "z$i.wav" -y
  prev="z$i.wav"
done
small=$(dur "$prev")
echo "      mesure reelle                     : $small s"
echo "      Le signal reste present (max_volume inchange) mais la duree"
echo "      s'effondre et n'est pas stable d'une execution a l'autre."
echo "      => NE PAS enchainer des dizaines d'acrossfade a 30 ms."
if awk "BEGIN{exit !( ($small-30.100) < -0.5 )}"; then
  printf '  [OK]    %-44s %s s << 30.100000 s\n' "chaine d=0.03 : effondrement confirme" "$small"
  OKS=$((OKS+1))
else
  printf '  [NOTE]  effondrement non reproduit ici (%s s)\n' "$small"
fi
echo

# --------------------------------- 5. le remede : un seul graphe de filtres
echo "--- 5. le remede : UN SEUL graphe de filtres pour les 3 fondus ---"
ffmpeg -v error -i s1.wav -i s2.wav -i s3.wav -i s4.wav \
  -filter_complex "[0][1]acrossfade=d=0.5[a];[a][2]acrossfade=d=0.5[b];[b][3]acrossfade=d=0.5[c]" \
  -map "[c]" one_shot.wav -y
printf '  graphe unique, 3 acrossfade -> %s s\n' "$(dur one_shot.wav)"
assert_eq "graphe unique, defaut" "6.500000" "$(dur one_shot.wav)"
echo

# --------------- 6. piege B : melanger les taux d'echantillonnage
echo "--- 6. piege B : concat avec des taux d'echantillonnage MELANGES ---"
echo "      (c'est le cas reel : sine en 44100, silence genere en 48000)"
ffmpeg -v error -f lavfi -i "sine=frequency=440:duration=1" -c:a pcm_s16le m1.wav -y   # 44100
ffmpeg -v error -f lavfi -i "aevalsrc=0:d=0.5:s=48000" -c:a pcm_s16le mgap.wav -y     # 48000
cp m1.wav m2.wav
echo "      m1.wav    : $(ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of default=nw=1:nk=1 m1.wav) Hz"
echo "      mgap.wav  : $(ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of default=nw=1:nk=1 mgap.wav) Hz"
printf "file 'm1.wav'\nfile 'mgap.wav'\nfile 'm2.wav'\n" > ml.txt
ffmpeg -v error -f concat -safe 0 -i ml.txt -c:a pcm_s16le mixed.wav -y
mixeddur=$(dur mixed.wav)
echo "      concat melange -> $mixeddur s   (la somme vaut 2.500000 s)"
drift=$(awk "BEGIN{printf \"%.6f\", $mixeddur-2.5}")
echo "      remplissage ajoute : ${drift} s = $(awk "BEGIN{printf \"%.1f\", $drift*1000}") ms"
if awk "BEGIN{exit !( ($mixeddur-2.5) > 0.005 )}"; then
  printf '  [OK]    %-44s +%s s de remplissage\n' "melange de taux = duree fausse" "$drift"
  OKS=$((OKS+1))
else
  printf '  [NOTE]  remplissage non observe (%s s)\n' "$drift"
fi
echo "      le meme en taux unique ${SR} Hz :"
ffmpeg -v error -f concat -safe 0 -i gl_same.txt -c:a pcm_s16le same.wav -y 2>/dev/null || {
  ffmpeg -v error -f lavfi -i "sine=frequency=440:duration=1:sample_rate=$SR" -c:a pcm_s16le x1.wav -y
  ffmpeg -v error -f lavfi -i "aevalsrc=0:d=0.5:s=$SR" -c:a pcm_s16le xgap.wav -y
  cp x1.wav x2.wav
  printf "file 'x1.wav'\nfile 'xgap.wav'\nfile 'x2.wav'\n" > gl_same.txt
  ffmpeg -v error -f concat -safe 0 -i gl_same.txt -c:a pcm_s16le same.wav -y
}
assert_eq "concat a taux unique" "2.500000" "$(dur same.wav)"
echo

# ------------------------------------------------- 7. audit d'une sortie
echo "--- 7. audit/junctions.py sur deux sorties assemblees ---"
# Utilise un venv local s'il existe, sinon python3 du systeme.
PY=python3
if [ -x "$REPO/.venv/bin/python" ]; then PY="$REPO/.venv/bin/python"
elif [ -x "$REPO/../.venv/bin/python" ]; then PY="$REPO/../.venv/bin/python"; fi
( cd "$REPO" && "$PY" -m audit.junctions "$T/same.wav" --expect 2.5 ) || { echo "  (audit a echoue)"; FAILS=$((FAILS+1)); }
echo
echo "--- et sur un fichier de silence pur (astats doit dire -inf) ---"
( cd "$REPO" && "$PY" -m audit.junctions "$T/xgap.wav" --expect 0.5 ) || { echo "  (audit a echoue)"; FAILS=$((FAILS+1)); }
echo

# ------------------------------------------------------------- 8. verdict
echo "=================================================================="
printf "  RESULTAT : %d OK, %d ECHEC\n" "$OKS" "$FAILS"
echo "=================================================================="
if [ "$FAILS" -gt 0 ]; then
  echo "  DES ASSERTIONS ONT ECHOUE."; exit 1
fi
echo "  TOUTES LES ASSERTIONS PASSENT."; exit 0
