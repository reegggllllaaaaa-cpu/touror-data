#!/usr/bin/env python
"""用 pyvalhalla（3.9.3，與 App 內建 valhalla-mobile 同版）把 PBF 建成 valhalla_tiles.tar。
設定同 驗證/desk-1/scripts/build.sh：get_config 預設值，只改 tile_extract／tile_dir／admin／timezone／concurrency。
用法：python -I build_tiles.py <in.pbf> <工作目錄>   → <工作目錄>/valhalla_tiles.tar"""
import json, os, shutil, subprocess, sys, time
import valhalla

def sh(cmd, log):
    print("$", " ".join(cmd), flush=True)
    with open(log, "w") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    if r.returncode:
        print(open(log).read()[-3000:]); sys.exit(f"失敗 {r.returncode}: {cmd[0]}")

def main():
    pbf, D = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    shutil.rmtree(D, ignore_errors=True); td = os.path.join(D, "valhalla_tiles"); os.makedirs(td)
    tar = os.path.join(D, "valhalla_tiles.tar"); open(tar, "w").close()
    c = valhalla.get_config(tile_extract=tar, tile_dir=td, verbose=False)
    c["mjolnir"]["admin"] = os.path.join(td, "admins.sqlite")
    c["mjolnir"]["timezone"] = os.path.join(td, "timezones.sqlite")
    c["mjolnir"]["concurrency"] = 4
    cj = os.path.join(D, "valhalla.json"); json.dump(c, open(cj, "w"), indent=1)
    binp = subprocess.check_output([sys.executable, "-m", "valhalla", "print_bin_path"], text=True).strip()
    for name in ("valhalla_build_admins", "valhalla_build_tiles"):
        t = time.time()
        sh(["/usr/bin/time", "-v", "-o", os.path.join(D, name + ".time"), os.path.join(binp, name), "-c", cj, pbf], os.path.join(D, name + ".log"))
        for l in open(os.path.join(D, name + ".time")):
            if "Maximum resident" in l: print(name, l.strip(), f"{time.time()-t:.0f}s", flush=True)
    paths = ["valhalla_tiles"]
    for r, ds, fs in os.walk(td):
        for x in ds + fs: paths.append(os.path.relpath(os.path.join(r, x), D))
    lf = os.path.join(D, "list.txt"); open(lf, "w").write("\n".join(sorted(set(paths))) + "\n")
    # 與 build.sh 同：排序後 tar --no-recursion
    subprocess.run(["tar", "cf", tar, "-C", D, "--no-recursion", "-T", lf], check=True)
    print("tar bytes", os.path.getsize(tar))

main()
