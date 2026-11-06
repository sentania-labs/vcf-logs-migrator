#!/usr/bin/env python3
"""Synthetic ESXi host syslog for N hosts over M days, backfilled into an
Aria/VCF Operations for Logs node through the agent-format ingest API
(timestampValidation:false keeps the original timestamps; PROVEN 8.18.7/9.0.2).

  LI_PASS=... ./gen_vsphere_hosts.py --host <8.x node> --hosts 24 --days 240 --total 2600000

Text looks like /var/log/syslog.log on ESXi 8:
  2026-05-04T15:54:41.123Z esx-a01.lab.example.com Hostd[2098101]: info hostd[...] [Originator@6876 sub=...] ...
Fields: hostname, appname, procid, severity, facility, cluster, site, esxi_build,
host_kind=esxi, dataset=synth-vsphere-8mo, incident=<kind> during incidents.
"""
import argparse, json, os, random, sys, time, math, datetime as dt
import requests, urllib3
urllib3.disable_warnings()
MAX_POST_BYTES = 3_000_000
SEV={"emerg":0,"alert":1,"crit":2,"err":3,"warning":4,"notice":5,"info":6,"debug":7}
FAC={"kern":0,"user":1,"daemon":3,"auth":4,"syslog":5,"cron":9,"local0":16,"local4":20}
BUILDS=["8.0.3 build 24022510","8.0.3 build 24280767","8.0.3 build 24585383"]
def r(): return random.random()
def pick(xs): return random.choice(xs)
def hx(n): return f"{random.getrandbits(n*4):0{n}x}"

def make_hosts(n):
    clusters=[("mgmt-cl01","dc1",4),("wld01-cl01","dc1",8),("wld01-cl02","dc2",6),("wld02-cl01","dc2",6),("edge-cl01","dc1",4)]
    hosts=[]; i=1
    for cname,site,size in clusters:
        for k in range(size):
            if len(hosts)>=n: break
            hosts.append((f"esx{i:02d}.lab.example.com",cname,site,pick(BUILDS),random.randint(2097000,2099999))); i+=1
    while len(hosts)<n: hosts.append((f"esx{i:02d}.lab.example.com","wld02-cl01","dc2",pick(BUILDS),random.randint(2097000,2099999))); i+=1
    return hosts

def opid(): return f"opID={hx(8)}-{hx(2)}-{hx(4)}"
VMS=[f"vm-{n}" for n in range(1,140)]
NAA=[f"naa.6000c29{hx(10)}" for _ in range(40)]

def ev_hostd(h):
    x=r()
    if x<0.25: return ("Hostd","info","local4",f"info hostd[{hx(7)}] [Originator@6876 sub=Vimsvc.TaskManager {opid()}] Task Created : haTask-{random.randint(1,400)}-vim.{pick(['VirtualMachine.createSnapshot','VirtualMachine.reconfigure','VirtualMachine.powerOn','VirtualMachine.removeSnapshot','host.StorageSystem.refresh','host.DatastoreSystem.refresh','VirtualMachine.relocate'])}-{random.randint(100000000,999999999)}")
    if x<0.45: return ("Hostd","info","local4",f"info hostd[{hx(7)}] [Originator@6876 sub=Vimsvc.TaskManager {opid()}] Task Completed : haTask-{random.randint(1,400)}-vim.{pick(['VirtualMachine.createSnapshot','VirtualMachine.reconfigure','host.StorageSystem.refresh','VirtualMachine.removeSnapshot'])}-{random.randint(100000000,999999999)} Status success")
    if x<0.60: return ("Hostd","info","local4",f"info hostd[{hx(7)}] [Originator@6876 sub=Vmsvc.vm:/vmfs/volumes/vsan:{hx(16)}-{hx(16)}/{hx(8)}-{hx(4)}/{pick(VMS)}.vmx] State Transition ({pick(['VM_STATE_ON -> VM_STATE_CREATE_SNAPSHOT','VM_STATE_CREATE_SNAPSHOT -> VM_STATE_ON','VM_STATE_ON -> VM_STATE_RECONFIGURING','VM_STATE_RECONFIGURING -> VM_STATE_ON','VM_STATE_OFF -> VM_STATE_POWERING_ON','VM_STATE_POWERING_ON -> VM_STATE_ON'])})")
    if x<0.70: return ("Hostd","info","local4",f"info hostd[{hx(7)}] [Originator@6876 sub=Default {opid()}] Accepted password for user {pick(['vpxuser','vpxuser','vpxuser','root'])} from {pick(['127.0.0.1','10.16.5.2','10.16.5.3'])}")
    if x<0.78: return ("Hostd","info","local4",f"info hostd[{hx(7)}] [Originator@6876 sub=Vimsvc.ha-eventmgr] Event {random.randint(100000,999999)} : User {pick(['vpxuser','root','dcui'])}@{pick(['127.0.0.1','10.16.5.2'])} logged {pick(['in','out'])} as {pick(['VMware vim-java 1.0','pyvmomi Python/3.11.9','VMware-client/8.0.0','hostd-probe'])}")
    if x<0.85: return ("Hostd","warning","local4",f"warning hostd[{hx(7)}] [Originator@6876 sub=Hostsvc.NetworkProvider] Failed to get vmknic info: vim.fault.NotFound")
    if x<0.92: return ("Hostd","info","local4",f"info hostd[{hx(7)}] [Originator@6876 sub=Statssvc.vim.PerformanceManager] Statistics collection {pick(['started','completed'])} for {random.randint(20,80)} entities in {random.randint(100,2200)} ms")
    return ("Hostd","info","local4",f"info hostd[{hx(7)}] [Originator@6876 sub=Libs {opid()}] Resource Pool /host/user: {pick(['memory','cpu'])} reservation {random.randint(0,4000)} limit -1 shares normal")

def ev_vpxa(h):
    x=r()
    if x<0.5: return ("Vpxa","info","local4",f"info vpxa[{hx(7)}] [Originator@6876 sub=vpxLro {opid()}] [VpxLRO] -- {pick(['BEGIN','FINISH'])} lro-{random.randint(100000,999999)} -- vpxa -- vpxapi.VpxaService.{pick(['fetchQuickStats','retrieveChanges','reserveManagedBy','setPowerState','getChanges','updateHeartbeat'])} -- {hx(8)}-{hx(4)}-{hx(4)}-{hx(4)}-{hx(12)}")
    if x<0.8: return ("Vpxa","info","local4",f"info vpxa[{hx(7)}] [Originator@6876 sub=vpxaHalCnxHostagent {opid()}] [WaitForUpdatesDone] Received callback; version {random.randint(100000,999999)}")
    if x<0.92: return ("Vpxa","info","local4",f"info vpxa[{hx(7)}] [Originator@6876 sub=vpxaVmprov {opid()}] Starting VM provisioning for {pick(VMS)}")
    return ("Vpxa","warning","local4",f"warning vpxa[{hx(7)}] [Originator@6876 sub=Default] Failed to get heartbeat for VM {pick(VMS)}: vim.fault.NotFound")

def ev_vmkernel(h):
    x=r(); cpu=random.randint(0,63); w=random.randint(2097000,2099999)
    if x<0.30: return ("vmkernel","info","kern",f"cpu{cpu}:{w})VSAN: {pick(['VsanDom','VsanLsom','VsanCmmds','VsanPlog'])}: {pick(['Owner for object changed','Component state updated to ACTIVE','Resync completed for object','Object %s owner elected'%hx(16),'Plog recovery completed for disk'])} {hx(16)}")
    if x<0.45: return ("vmkernel","info","kern",f"cpu{cpu}:{w})NetPort: {random.randint(1000,2000)}: {pick(['enabled','disabled'])} port 0x{hx(7)} with mac 00:50:56:{hx(2)}:{hx(2)}:{hx(2)}")
    if x<0.55: return ("vmkernel","info","kern",f"cpu{cpu}:{w})ScsiDeviceIO: {random.randint(3000,4500)}: Cmd(0x{hx(12)}) 0x{pick(['28','2a','89','1a','85'])}, CmdSN 0x{hx(5)} from world {w} to dev \"{pick(NAA)}\" failed H:0x0 D:0x2 P:0x0 Valid sense data: 0x5 0x24 0x0.")
    if x<0.65: return ("vmkernel","warning","kern",f"cpu{cpu}:{w})WARNING: vmw_ahci[0000:00:1f.2]: ahciAbortIO:(curr) HWQD: {random.randint(0,31)} BusyL: 0")
    if x<0.75: return ("vmkernel","info","kern",f"cpu{cpu}:{w})StorageApdHandler: {random.randint(1000,1300)}: APD Handle {hx(8)}-{hx(8)} {pick(['Created','Freed'])} with lock[StorageApd-0x{hx(10)}]")
    if x<0.85: return ("vmkernel","info","kern",f"cpu{cpu}:{w})Tcpip_Vmk: {random.randint(1000,1300)}: {pick(['vmk0','vmk1','vmk2','vmk3'])}: {pick(['link down -> up','ARP announce','MTU set to 9000'])}")
    if x<0.93: return ("vmkernel","info","kern",f"cpu{cpu}:{w})Vmxnet3: {random.randint(10000,30000)}: Using default queue delivery for {pick(VMS)} eth0")
    return ("vmkernel","info","kern",f"cpu{cpu}:{w})MemSched: {random.randint(10000,30000)}: vm {w}: {random.randint(500,16000)} MB mapped, {random.randint(0,2000)} MB swapped, {random.randint(0,8000)} MB ballooned")

def ev_misc(h):
    x=r()
    if x<0.20: return ("Rhttpproxy","info","local4",f"info rhttpproxy[{hx(7)}] [Originator@6876 sub=Proxy Req {random.randint(10000,99999)}] New proxy client SSL(TCP(local={pick(['10.16.5.11','10.16.5.12','10.16.5.13'])}:443, peer=10.16.5.2:{random.randint(30000,65000)}))")
    if x<0.35: return ("Fdm","info","local4",f"info fdm[{hx(7)}] [Originator@6876 sub=Cluster {opid()}] [ClusterManagerImpl::{pick(['UpdateMasterHostStateInfo','ProcessElectionMsg','AddHost'])}] {pick(['Master state updated','Election message processed','Slave heartbeat received'])} host-{random.randint(10,80)}")
    if x<0.45: return ("vobd","notice","local0",f"[{pick(['vmfsCorrelator','netCorrelator','scsiCorrelator','GenericCorrelator'])}] {random.randint(1000000,9999999)}us: [{pick(['esx.audit.vmfs.volume.mounted','esx.clear.net.vmnic.linkstate.up','esx.audit.host.boot','esx.audit.ssh.enabled','esx.audit.maintenancemode.entered','esx.audit.maintenancemode.exited','vob.vsan.pdl.offline'])}] {pick(['vmnic1 linkstate is up.','File system vsanDatastore on volume mounted.','SSH access has been enabled.','The host has entered maintenance mode.','The host has exited maintenance mode.'])}")
    if x<0.55: return ("crond","info","cron",f"USER root pid {random.randint(2097000,2099999)} cmd /usr/lib/vmware/vmksummary/log-heartbeat.py")
    if x<0.65: return ("sfcbd","info","daemon",f"sfcb-vmware_base[{hx(7)}]: VICimProvider {pick(['exiting on EOF','started','polling sensors'])}")
    if x<0.75: return ("clomd","info","local4",f"info clomd[{hx(7)}] [Originator@6876 sub=Default] CLOMRebalance{pick(['Check','Run'])}: {pick(['Disk balance within threshold','Rebalancing object','No rebalance needed'])} {hx(16)}")
    if x<0.85: return ("vsanmgmtd","info","local4",f"info vsanmgmtd[{hx(7)}] [Originator@6876 sub=VsanHealth] {pick(['Health check group network passed','Cluster health summary refreshed','Collected 48 health checks'])} in {random.randint(100,9000)} ms")
    if x<0.92: return ("hostd-probe","info","local4",f"info hostd-probe[{hx(7)}] [Originator@6876 sub=Default] hostd detected to be {pick(['alive','alive','alive','non-responsive'])}")
    return ("sshd","info","auth",f"{pick(['Accepted keyboard-interactive/pam for root from 10.16.3.13 port %d ssh2'%random.randint(30000,65000),'Session opened for root','Connection closed by 10.16.3.13 port %d'%random.randint(30000,65000),'Received disconnect from 10.16.3.13'])}")

APPS=[(ev_hostd,3.0),(ev_vpxa,2.5),(ev_vmkernel,2.0),(ev_misc,2.5)]

def incident_event(kind,h):
    cpu=random.randint(0,63); w=random.randint(2097000,2099999)
    if kind=="apd": return ("vmkernel","warning","kern",f"cpu{cpu}:{w})WARNING: {pick(['NMP: nmp_PathDetermineFailure:','StorageApdHandler: 1200: Device or filesystem with identifier','ScsiDeviceIO: 4176: Cmd'])} {pick(NAA)} has entered the All Paths Down state.")
    if kind=="vsan_latency": return ("vobd","warning","local0",f"[scsiCorrelator] {random.randint(1000000,9999999)}us: [esx.problem.scsi.device.io.latency.high] Device {pick(NAA)} performance has deteriorated. I/O latency increased from average value of {random.randint(800,2500)} microseconds to {random.randint(30000,120000)} microseconds.")
    if kind=="nic_flap": return ("vmkernel","err","kern",f"cpu{cpu}:{w}){pick(['vmnic0','vmnic1','vmnic2','vmnic3'])}: NIC Link is {pick(['Down','Up 25 Gbps Full Duplex'])}")
    if kind=="maintenance": return (pick(["Hostd","vobd","Fdm"]),"info","local4",pick([f"info hostd[{hx(7)}] [Originator@6876 sub=Vimsvc.TaskManager {opid()}] Task Created : haTask-ha-host-vim.HostSystem.enterMaintenanceMode-{random.randint(100000000,999999999)}",f"info hostd[{hx(7)}] [Originator@6876 sub=Vmsvc.vm:/vmfs/volumes/vsan:{hx(16)}/{pick(VMS)}.vmx {opid()}] VMotion {pick(['prepare','start','completed'])} for migration id {random.randint(10**15,10**16)}",f"[GenericCorrelator] {random.randint(1000000,9999999)}us: [esx.audit.maintenancemode.entered] The host has entered maintenance mode.",f"info fdm[{hx(7)}] [Originator@6876 sub=Invt] [HostStateChange] host-{random.randint(10,80)} entering maintenance mode",f"info hostd[{hx(7)}] [Originator@6876 sub=Vimsvc.ha-eventmgr] Event {random.randint(100000,999999)} : Host {h[0]} in {h[1]} has exited maintenance mode"]))
    if kind=="hostd_hang": return (pick(["hostd-probe","Hostd","Vpxa"]),"err","local4",pick([f"error hostd-probe[{hx(7)}] [Originator@6876 sub=Default] hostd detected to be non-responsive",f"warning hostd[{hx(7)}] [Originator@6876 sub=ThreadPool] Thread pool on fire; {random.randint(60,120)} tasks pending",f"error vpxa[{hx(7)}] [Originator@6876 sub=Default] Unable to connect to hostd: Connection refused",f"error hostd[{hx(7)}] [Originator@6876 sub=Hostsvc] Memory checker: current value {random.randint(1000,1400)} MB exceeds soft limit {random.randint(900,1000)} MB"]))
    if kind=="psod_reboot": return (pick(["vmkernel","vobd","Hostd"]),"crit","kern",pick([f"cpu{cpu}:{w})@BlueScreen: #PF Exception 14 in world {w}:{pick(['vmm0:'+pick(VMS),'helper1-0','HBR-helper'])} IP 0x{hx(12)} addr 0x0",f"cpu{cpu}:{w})Panic: {random.randint(500,900)}: Halting PCPU {cpu}",f"[GenericCorrelator] {random.randint(1000000,9999999)}us: [esx.audit.host.boot] Host has booted.",f"info hostd[{hx(7)}] [Originator@6876 sub=Default] Hostd starting up (build {random.randint(24000000,24700000)})"]))
    return ("vmkernel","warning","kern",f"cpu{cpu}:{w})WARNING: MemSched: {random.randint(10000,30000)}: Host memory low: {random.randint(1,4)}% free, swapping vm {w}")

def rate_at(t, base):
    lh=(t.hour-6)%24
    diurnal=0.7+0.3*math.exp(-((lh-14)/5.0)**2)
    wk=0.85 if t.weekday()>=5 else 1.0
    return base*diurnal*wk

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--host",required=True); ap.add_argument("--user",default="admin")
    ap.add_argument("--hosts",type=int,default=24); ap.add_argument("--days",type=int,default=240); ap.add_argument("--total",type=int,default=2_600_000)
    ap.add_argument("--agent-id",default="synth-vsphere"); ap.add_argument("--seed",type=int,default=20261104)
    ap.add_argument("--end-offset-min",type=int,default=15); ap.add_argument("--dataset",default="synth-vsphere-8mo"); ap.add_argument("--start-date",default=None); ap.add_argument("--extra-field",action="append",default=[]); ap.add_argument("--dry-run",action="store_true"); ap.add_argument("--sample",type=int,default=0)
    a=ap.parse_args(); random.seed(a.seed); rng=random.Random(a.seed+7)
    HOSTS=make_hosts(a.hosts)
    end=dt.datetime.now(dt.timezone.utc).replace(second=0,microsecond=0)-dt.timedelta(minutes=a.end_offset_min)
    start=end-dt.timedelta(days=a.days) if not a.start_date else dt.datetime.fromisoformat(a.start_date).replace(tzinfo=dt.timezone.utc)
    if a.start_date: end=start+dt.timedelta(days=a.days)
    minutes=a.days*1440; probe=sum(rate_at(start+dt.timedelta(minutes=m),1.0) for m in range(0,minutes,7))*7; base=a.total/probe
    # incidents: monthly patch windows (whole cluster, maintenance), plus faults
    incs=[]
    clusters=sorted({h[1] for h in HOSTS})
    d=start+dt.timedelta(days=rng.uniform(5,25))
    while d<end:
        c=rng.choice(clusters); t0=d.replace(hour=2,minute=0)
        for i,h in enumerate(HOSTS):
            if h[1]==c: incs.append((t0+dt.timedelta(minutes=35*i),t0+dt.timedelta(minutes=35*i+30),i,"maintenance"))
        d+=dt.timedelta(days=rng.uniform(26,34))
    kinds=["apd","vsan_latency","nic_flap","hostd_hang","psod_reboot","mem_pressure"]
    for i in range(18):
        t0=start+dt.timedelta(days=rng.uniform(2,a.days-1),hours=rng.uniform(0,24)); incs.append((t0,t0+dt.timedelta(minutes=rng.uniform(5,90)),rng.randrange(len(HOSTS)),kinds[i%len(kinds)]))
    tok=None
    if not (a.dry_run or a.sample):
        r_=requests.post(f"https://{a.host}:9543/api/v2/sessions",json={"username":a.user,"password":os.environ["LI_PASS"],"provider":"Local"},verify=False,timeout=30); r_.raise_for_status(); tok=r_.json()["sessionId"]
    batch=[]; size=2; sent=0; nbytes=0; t_begin=time.time(); last=t_begin
    def flush():
        nonlocal batch,size,sent,tok
        if not batch: return
        if not (a.dry_run or a.sample):
            body={"timestampValidation":False,"messages":[dict(e,id=i+1) for i,e in enumerate(batch)]}
            for attempt in range(10):
                try: r_=requests.post(f"https://{a.host}:9543/api/v2/events/ingest/{a.agent_id}",json=body,headers={"Authorization":f"Bearer {tok}"},verify=False,timeout=180)
                except requests.RequestException as ex: print("post error",ex,file=sys.stderr); time.sleep(15); continue
                if r_.status_code==503: time.sleep(int(r_.headers.get("Retry-After","5"))); continue
                if r_.status_code==401:
                    r2=requests.post(f"https://{a.host}:9543/api/v2/sessions",json={"username":a.user,"password":os.environ["LI_PASS"],"provider":"Local"},verify=False,timeout=30); r2.raise_for_status(); tok=r2.json()["sessionId"]; continue
                r_.raise_for_status(); break
            else: raise SystemExit("ingest kept failing")
        sent+=len(batch); batch=[]; size=2
    hw=[1.0]*len(HOSTS); t=start; shown=0
    while t<end:
        lam=rate_at(t,base)*(0.9+0.2*random.random()); n=max(0,int(random.gauss(lam,max(1,lam**0.5))))
        active=[(ix,k) for (t0,t1,ix,k) in incs if t0<=t<t1]
        for _ in range(n):
            if active and random.random()<0.5:
                ix,k=pick(active); h=HOSTS[ix]; appname,sev,fac,msg=incident_event(k,h); inc=k
            else:
                h=random.choices(HOSTS,weights=hw)[0]; gen=random.choices([g for g,_ in APPS],weights=[w for _,w in APPS])[0]; appname,sev,fac,msg=gen(h); inc=None
            ts=t+dt.timedelta(seconds=random.uniform(0,60))
            pid=h[4]+(hash(appname)%500)
            text=f"{ts:%Y-%m-%dT%H:%M:%S}.{ts.microsecond//1000:03d}Z {h[0]} {appname}[{pid}]: {msg}"
            fields=[{"name":"hostname","content":h[0]},{"name":"appname","content":appname},{"name":"procid","content":str(pid)},{"name":"severity","content":sev},{"name":"facility","content":fac},{"name":"syslog_pri","content":str(FAC[fac]*8+SEV[sev])},{"name":"cluster","content":h[1]},{"name":"site","content":h[2]},{"name":"esxi_build","content":h[3]},{"name":"host_kind","content":"esxi"},{"name":"dataset","content":a.dataset}]+[{"name":k,"content":v} for k,v in (x.split("=",1) for x in a.extra_field)]
            if inc: fields.append({"name":"incident","content":inc})
            e={"text":text,"timestamp":int(ts.timestamp()*1000),"fields":fields}
            if a.sample:
                print(text); shown+=1
                if shown>=a.sample: return
            s=len(json.dumps(e))+1; nbytes+=len(text)+1
            if size+s>MAX_POST_BYTES: flush()
            batch.append(e); size+=s
        t+=dt.timedelta(minutes=1)
        if time.time()-last>60:
            el=time.time()-t_begin; print(f"{dt.datetime.now():%H:%M:%S} at {t:%Y-%m-%d %H:%M}Z sent={sent} raw={nbytes/1e6:.0f}MB rate={sent/el*60:.0f}/min",flush=True); last=time.time()
    flush()
    print(f"DONE sent={sent} raw_text_bytes={nbytes} hosts={len(HOSTS)} window {start:%Y-%m-%d}..{end:%Y-%m-%d %H:%M}Z faults={[(t0.strftime('%m-%d %H:%M'),HOSTS[ix][0],k) for t0,t1,ix,k in incs if k!='maintenance']} maintenance_windows={len([1 for *_,k in incs if k=='maintenance'])}",flush=True)
if __name__=="__main__": main()
