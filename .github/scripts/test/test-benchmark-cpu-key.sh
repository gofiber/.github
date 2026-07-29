#!/usr/bin/env bash
# Tests the benchmark baseline key logic embedded in benchmark.yml: the GOMAXPROCS
# pin, the hardware description (including the arm64 fallback), the mixed-hardware
# join, and the cache-key isolation that keeps a mixed run from inheriting a
# single-machine baseline.
# Run from anywhere: bash .github/scripts/test/test-benchmark-cpu-key.sh

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORKFLOW="$SCRIPT_DIR/../../workflows/benchmark.yml"
REPORT_ACTION="$SCRIPT_DIR/../../actions/benchmark-report/action.yml"

fails=0
check() {
  if [ "$2" = "$3" ]; then
    echo "ok   $1"
  else
    echo "FAIL $1"
    echo "       want: $2"
    echo "       got:  $3"
    fails=$((fails + 1))
  fi
}

SANDBOX=$(mktemp -d)
trap 'rm -rf "$SANDBOX"' EXIT

cat > "$SANDBOX/lscpu-x86.txt" <<'EOF'
Architecture:                       x86_64
Vendor ID:                          AuthenticAMD
Model name:                         AMD EPYC
Thread(s) per core:                 1
EOF

# what lscpu prints when it cannot decode the ARM part id: no Model name line at all
cat > "$SANDBOX/lscpu-arm.txt" <<'EOF'
Architecture:                       aarch64
CPU op-mode(s):                     32-bit, 64-bit
Vendor ID:                          ARM
BogoMIPS:                           50.00
EOF

cat > "$SANDBOX/cpuinfo-arm.txt" <<'EOF'
processor	: 0
BogoMIPS	: 50.00
CPU implementer	: 0x41
CPU part	: 0xd0c
processor	: 1
BogoMIPS	: 50.00
CPU implementer	: 0x41
CPU part	: 0xd0c
EOF

# mirrors the `Describe benchmark hardware` step
pin() {
  if [[ "$1" =~ -([0-9]+)vcpu- ]]; then printf '%s' "${BASH_REMATCH[1]}"; fi
}

describe() { # $1 lscpu fixture, $2 cpuinfo fixture, $3 arch
  local model
  model="$(awk -F': *' '/^Model name/ && !m { m = $2 } END { print m }' "$1")"
  if [[ -z "$model" ]]; then
    model="$3 $(awk -F': *' '/^CPU implementer|^CPU part/ { print $2 }' "$2" | sort -u | tr '\n' ' ')"
  fi
  printf '%s' "$(printf '%s' "$model" | sed 's/ *$//')"
}

# mirrors the merge step's cpu= line
join_models() { (IFS=+; printf '%s' "$*"); }

# mirrors SAFE_CPU_MODEL in benchmark-report/action.yml
slug() {
  local s
  s="$(printf '%s' "$1" | tr -cs '[:alnum:]._-' '-')"
  s="${s#-}"
  printf '%s' "${s%-}"
}

key() { printf 'benchmark-v2-Linux-%s-%s-' "$(slug "$1")" "$(slug "$2")"; }

echo "--- GOMAXPROCS pin"
check "4vcpu label"      "4"  "$(pin blacksmith-4vcpu-ubuntu-2404)"
check "2vcpu label"      "2"  "$(pin blacksmith-2vcpu-ubuntu-2404)"
check "arm label"        "4"  "$(pin blacksmith-4vcpu-ubuntu-2404-arm)"
check "two-digit label"  "16" "$(pin blacksmith-16vcpu-ubuntu-2204)"
check "non-blacksmith"   ""   "$(pin ubuntu-latest)"

echo "--- hardware description"
check "x86 uses the model name" "AMD EPYC" \
  "$(describe "$SANDBOX/lscpu-x86.txt" "$SANDBOX/cpuinfo-arm.txt" x86_64)"
# without the fallback this would be empty and every arm64 runner would share one key
check "arm falls back to raw ids" "aarch64 0x41 0xd0c" \
  "$(describe "$SANDBOX/lscpu-arm.txt" "$SANDBOX/cpuinfo-arm.txt" aarch64)"
check "arm description is never empty" "no" \
  "$([ -z "$(describe "$SANDBOX/lscpu-arm.txt" "$SANDBOX/cpuinfo-arm.txt" aarch64)" ] && echo yes || echo no)"
# a second core generation must not collapse onto the same signature
cat > "$SANDBOX/cpuinfo-arm2.txt" <<'EOF'
processor	: 0
CPU implementer	: 0x41
CPU part	: 0xd4f
EOF
check "different arm core, different signature" "different" \
  "$([ "$(describe "$SANDBOX/lscpu-arm.txt" "$SANDBOX/cpuinfo-arm.txt" aarch64)" \
     != "$(describe "$SANDBOX/lscpu-arm.txt" "$SANDBOX/cpuinfo-arm2.txt" aarch64)" ] \
     && echo different || echo same)"

echo "--- mixed-hardware join"
AMD="AMD EPYC (GOMAXPROCS=4)"
INTEL="Intel(R) Xeon(R) Processor (GOMAXPROCS=4)"
check "single model unchanged" "$AMD"        "$(join_models "$AMD")"
check "two models joined"      "$AMD+$INTEL" "$(join_models "$AMD" "$INTEL")"

echo "--- cache-key isolation (restore-keys is a prefix match)"
AMD_KEY="$(key "$AMD" .)"
MIX_KEY="$(key "$(join_models "$AMD" "$INTEL")" .)"
check "mixed key differs from single" "different" \
  "$([ "$AMD_KEY" != "$MIX_KEY" ] && echo different || echo same)"
# the regression this guards: a mixed run must not restore the AMD baseline
check "mixed prefix does not match AMD cache" "no" \
  "$(case "${AMD_KEY}sha" in "$MIX_KEY"*) echo yes ;; *) echo no ;; esac)"
check "AMD prefix does not match mixed cache" "no" \
  "$(case "${MIX_KEY}sha" in "$AMD_KEY"*) echo yes ;; *) echo no ;; esac)"
# a differently sized runner must not reuse the 4-core baseline either
check "GOMAXPROCS change splits the key" "different" \
  "$([ "$AMD_KEY" != "$(key "AMD EPYC (GOMAXPROCS=8)" .)" ] && echo different || echo same)"

echo "--- sources still carry the logic under test"
check "workflow pins GOMAXPROCS" "yes" \
  "$(grep -qF 'echo "GOMAXPROCS=${procs}" >> "$GITHUB_ENV"' "$WORKFLOW" && echo yes || echo no)"
check "workflow exports BENCHMARK_CPU" "yes" \
  "$(grep -qF 'BENCHMARK_CPU=${model} (GOMAXPROCS=${procs})' "$WORKFLOW" && echo yes || echo no)"
check "workflow keeps the arm fallback" "yes" \
  "$(grep -qF '/^CPU implementer|^CPU part/' "$WORKFLOW" && echo yes || echo no)"
check "shard records BENCHMARK_CPU" "yes" \
  "$(grep -qF 'printf '"'"'%s\n'"'"' "$BENCHMARK_CPU" > cpu.txt' "$WORKFLOW" && echo yes || echo no)"
check "single-job path forwards it" "yes" \
  "$(grep -qF 'cpu-model: ${{ env.BENCHMARK_CPU }}' "$WORKFLOW" && echo yes || echo no)"
check "workflow joins every model" "yes" \
  "$(grep -qF 'cpu=$(IFS=+' "$WORKFLOW" && echo yes || echo no)"
# key() above mirrors the action; a bumped prefix there must be bumped here too
check "action still builds the v2 prefix" "yes" \
  "$(grep -qF 'prefix=benchmark-v2-${{ runner.os }}-' "$REPORT_ACTION" && echo yes || echo no)"

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails check(s) failed"
  exit 1
fi
echo "all checks passed"
