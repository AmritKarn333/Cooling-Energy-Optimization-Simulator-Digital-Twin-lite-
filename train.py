
import json, os, random, subprocess, sys, tempfile
from pathlib import Path

HERE=Path(__file__).resolve().parent
CFG=HERE/"config.cfg"
EXE=HERE/("thermal_sim.exe" if os.name=="nt" else "thermal_sim")
OUT=HERE/"temperature.csv"

def compile_sim():
    if not EXE.exists():
        subprocess.run(["g++","-O2","-std=c++17",str(HERE/"thermal_sim.cpp"),"-o",str(EXE)],check=True)

def read_config():
    return CFG.read_text(encoding="utf-8")

def write_config(text):
    CFG.write_text(text,encoding="utf-8")

def run_sim():
    p=subprocess.run([str(EXE),str(CFG),str(OUT)],capture_output=True,text=True,check=True,cwd=HERE)
    return json.loads(p.stdout.strip().splitlines()[-1])

def random_action(cfg_text):
    """Example action: move the cooling point along the right/bottom wall.
    A real RL policy should output legal component coordinates and keep other settings fixed.
    """
    lines=cfg_text.splitlines()
    out=[]
    for line in lines:
        if line.strip().startswith("cooling="):
            a=line.split("=",1)[1].split(",")
            # Keep the cooling line on the right edge; change vertical position.
            a[0]="97"; a[1]=str(random.randint(5,65)); a[2]="3"; a[3]=str(random.randint(20,35))
            line="cooling="+",".join(a)
        out.append(line)
    return "\n".join(out)+"\n"

def main():
    compile_sim()
    original=read_config()
    best=None
    try:
        for episode in range(20):
            candidate=random_action(original)
            write_config(candidate)
            metrics=run_sim()
            reward=-(metrics["average_rack_temp"]+0.5*metrics["max_rack_temp"])
            print(f"episode={episode:02d} reward={reward:.3f} avg={metrics['average_rack_temp']:.3f} max={metrics['max_rack_temp']:.3f}")
            if best is None or reward>best[0]:
                best=(reward,candidate,metrics)
        if best:
            write_config(best[1]); run_sim()
            print("\nBest candidate retained in config.cfg:",best[2])
    finally:
        # Keep the best config if one was found; otherwise restore original.
        if best is None: write_config(original)

if __name__=="__main__":main()
