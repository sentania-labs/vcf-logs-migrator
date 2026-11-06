#!/usr/bin/env python3
"""Generate N months of realistic multi-host syslog and backfill it into an
Aria/VCF Operations for Logs node through the agent-format ingest API, which
keeps the original timestamps (PROVEN on 8.18.7 and 9.0.2 on 2026-10-30).

  LI_PASS=... ./gen_syslog_6mo.py --host <8.x node> --days 184 [--total 1400000] [--dry-run]

Events carry fields hostname, appname, procid, severity, facility, environment,
site, dataset=synth-syslog-6mo so they can be filtered or ignored later.
"""
import argparse, json, os, random, sys, time, math, datetime as dt
import requests, urllib3
urllib3.disable_warnings()
MAX_POST_BYTES = 3_000_000

HOSTS = [  # name, kind, site, env
 ("web01","web","dc1","prod"),("web02","web","dc1","prod"),("web03","web","dc2","prod"),
 ("app01","app","dc1","prod"),("app02","app","dc2","prod"),("app-stg01","app","dc1","staging"),
 ("db01","db","dc1","prod"),("db02","db","dc2","prod"),
 ("esx-a01","esxi","dc1","prod"),("esx-a02","esxi","dc1","prod"),("esx-b01","esxi","dc2","prod"),
 ("fw-edge01","firewall","dc1","prod"),("sw-core01","switch","dc1","prod"),("nas01","nas","dc1","prod"),
]
USERS=["alice","deploy","backup","svc-monitor","jdoe","ansible","root"]
IPS=["10.20.1.%d"%i for i in range(10,60)]+["10.16.9.%d"%i for i in range(5,40)]
URLS=["/","/api/v1/items","/api/v1/login","/static/app.js","/health","/api/v1/orders","/img/logo.png","/admin","/api/v2/search"]
SEV={"emerg":0,"alert":1,"crit":2,"err":3,"warning":4,"notice":5,"info":6,"debug":7}
FAC={"kern":0,"user":1,"mail":2,"daemon":3,"auth":4,"syslog":5,"cron":9,"authpriv":10,"local0":16,"local4":20}

def r(): return random.random()
def pick(xs): return random.choice(xs)

def linux_common(h):
    x=r()
    if x<0.28: return ("sshd",random.randint(1000,65000),"info","auth",
        f"Accepted publickey for {pick(USERS)} from {pick(IPS)} port {random.randint(30000,65000)} ssh2: ED25519 SHA256:{random.getrandbits(64):016x}")
    if x<0.33: return ("sshd",random.randint(1000,65000),"info","auth",f"Disconnected from user {pick(USERS)} {pick(IPS)} port {random.randint(30000,65000)}")
    if x<0.36: return ("sshd",random.randint(1000,65000),"warning","auth",f"Failed password for invalid user {pick(['admin','test','oracle','ubuntu','pi'])} from {pick(IPS)} port {random.randint(30000,65000)} ssh2")
    if x<0.50: return ("CRON",random.randint(1000,65000),"info","cron",f"({pick(['root','backup','svc-monitor'])}) CMD ({pick(['/usr/local/bin/check-disk.sh','/usr/lib/sysstat/sa1 1 1','run-parts /etc/cron.hourly','/opt/backup/snap.sh --quiet','/usr/bin/certbot renew -q'])})")
    if x<0.62: return ("systemd",1,"info","daemon",pick([f"Started {pick(['Session 4821 of User alice','Daily apt download activities','Cleanup of Temporary Directories','Logrotate','Telegraf Agent'])}.",f"Finished {pick(['Daily apt upgrade and clean activities','Rotate log files','Discard unused blocks'])}.",f"Starting {pick(['Refresh fwupd metadata','Daily man-db regeneration'])}...",f"{pick(['telegraf','salt-minion','chronyd'])}.service: Deactivated successfully."]))
    if x<0.70: return ("kernel",None,"info","kern",pick([f"[UFW BLOCK] IN=ens192 OUT= MAC=00:50:56:a1:{random.randint(0,255):02x}:{random.randint(0,255):02x}:{random.randint(0,255):02x} SRC={pick(IPS)} DST=10.20.1.{random.randint(10,60)} LEN=60 TOS=0x00 TTL=63 PROTO=TCP SPT={random.randint(1024,65000)} DPT={pick([22,443,3389,5432,8080])} SYN",f"eth0: link up, 10Gbps, full-duplex",f"audit: type=1400 apparmor=\"DENIED\" operation=\"open\" profile=\"snap.lxd.daemon\" name=\"/proc/{random.randint(100,9000)}/status\"",f"TCP: request_sock_TCP: Possible SYN flooding on port 443. Sending cookies."]))
    if x<0.78: return ("chronyd",random.randint(500,900),"info","daemon",pick([f"Selected source 10.16.1.{random.randint(1,4)} (ntp.example.internal)",f"System clock wrong by {random.uniform(-0.004,0.004):.6f} seconds","Source 10.16.1.2 offline"]))
    if x<0.84: return ("sudo",None,"notice","authpriv",f"{pick(USERS)} : TTY=pts/{random.randint(0,4)} ; PWD=/home/{pick(USERS)} ; USER=root ; COMMAND={pick(['/usr/bin/systemctl restart nginx','/usr/bin/apt update','/usr/bin/journalctl -u postgresql','/usr/bin/tail -f /var/log/syslog','/usr/sbin/reboot'])}")
    if x<0.90: return ("telegraf",random.randint(900,1200),"info","daemon",pick(["[agent] Successfully connected to outputs.influxdb_v2",f"[inputs.procstat] Could not find PID for pattern 'nginx'","[outputs.http] Buffer fullness: 0 / 10000 metrics"]))
    if x<0.95: return ("salt-minion",random.randint(1200,1600),"info","daemon",pick(["Returning information for job: 2026{:02d}{:02d}1{:02d}3456".format(random.randint(1,12),random.randint(1,28),random.randint(0,9)),"Executing command state.apply with jid","Minion is ready to receive requests!"]))
    return ("rsyslogd",None,"info","syslog",pick(["[origin software=\"rsyslogd\" swVersion=\"8.2312.0\" x-pid=\"712\"] rsyslogd was HUPed","action 'action-1-builtin:omfile' resumed (module 'builtin:omfile')"]))

def web(h):
    if r()<0.45:
        code=random.choices([200,200,200,200,301,304,404,500,502],weights=[60,20,10,5,3,4,3,1,1])[0]
        sev="info" if code<400 else ("warning" if code<500 else "err")
        return ("nginx",random.randint(1300,1400),sev,"local0",f'{pick(IPS)} - - "{pick(["GET","GET","GET","POST","PUT"])} {pick(URLS)} HTTP/1.1" {code} {random.randint(120,98000)} "-" "{pick(["Mozilla/5.0 (Windows NT 10.0; Win64; x64)","curl/8.5.0","Go-http-client/1.1","Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)"])}" rt={random.uniform(0.002,1.8):.3f}')
    if r()<0.1: return ("nginx",random.randint(1300,1400),"err","local0",f"*{random.randint(1,999)} upstream timed out (110: Connection timed out) while reading response header from upstream, client: {pick(IPS)}, server: app.example.com, request: \"GET {pick(URLS)} HTTP/1.1\", upstream: \"http://10.20.2.{random.randint(10,12)}:8080{pick(URLS)}\"")
    return linux_common(h)

def app(h):
    if r()<0.5:
        lvl=random.choices(["INFO","INFO","INFO","WARN","ERROR"],weights=[70,10,10,7,3])[0]
        sev={"INFO":"info","WARN":"warning","ERROR":"err"}[lvl]
        msg=pick([f"request_id={random.getrandbits(48):012x} method=GET path={pick(URLS)} status=200 duration_ms={random.randint(3,900)}",f"order {random.randint(100000,999999)} created for customer {random.randint(1000,9999)} total={random.uniform(5,900):.2f}",f"cache miss key=user:{random.randint(1,9000)}:profile",f"pool exhausted, waited {random.randint(500,5000)}ms for connection",f"retrying upstream call to payments (attempt {random.randint(1,3)}/3)",f"java.net.SocketTimeoutException: Read timed out at com.example.app.client.PaymentsClient.call(PaymentsClient.java:{random.randint(80,240)})",f"scheduled job nightly-report finished in {random.randint(40,900)}s rows={random.randint(1000,900000)}","JVM GC pause {}ms (G1 Young)".format(random.randint(5,400))])
        return ("orders-api",random.randint(2000,2100),sev,"local4",f"{lvl} [{pick(['http-nio-8080-exec-%d'%random.randint(1,40),'scheduler-1','main'])}] {msg}")
    return linux_common(h)

def db(h):
    if r()<0.5:
        x=r()
        if x<0.45: return ("postgres",random.randint(3000,3900),"info","local0",f"[{random.randint(1,99999)}] LOG:  duration: {random.uniform(1,4800):.3f} ms  statement: {pick(['SELECT * FROM orders WHERE customer_id = $1','UPDATE inventory SET qty = qty - $1 WHERE sku = $2','INSERT INTO events (ts, kind, payload) VALUES ($1,$2,$3)','SELECT count(*) FROM sessions WHERE expires_at < now()','VACUUM ANALYZE public.events'])}")
        if x<0.6: return ("postgres",random.randint(3000,3900),"info","local0",f"[{random.randint(1,99999)}] LOG:  checkpoint complete: wrote {random.randint(100,9000)} buffers ({random.uniform(0.1,9):.1f}%); 0 WAL file(s) added, {random.randint(0,4)} removed, {random.randint(0,6)} recycled; write={random.uniform(1,200):.3f} s, sync={random.uniform(0.001,0.5):.3f} s")
        if x<0.7: return ("postgres",random.randint(3000,3900),"info","local0",f"[{random.randint(1,99999)}] LOG:  automatic vacuum of table \"app.public.{pick(['events','orders','sessions','audit_log'])}\": index scans: 1 pages: 0 removed, {random.randint(100,90000)} remain tuples: {random.randint(0,50000)} removed")
        if x<0.8: return ("postgres",random.randint(3000,3900),"warning","local0",f"[{random.randint(1,99999)}] WARNING:  there is already a transaction in progress")
        if x<0.9: return ("postgres",random.randint(3000,3900),"err","local0",f"[{random.randint(1,99999)}] ERROR:  deadlock detected\n\tDETAIL:  Process {random.randint(3000,3900)} waits for ShareLock on transaction {random.randint(100000,999999)}; blocked by process {random.randint(3000,3900)}.")
        return ("postgres",random.randint(3000,3900),"err","local0",f"[{random.randint(1,99999)}] FATAL:  password authentication failed for user \"{pick(['app','readonly','grafana','reporting'])}\"")
    return linux_common(h)

def esxi(h):
    x=r()
    if x<0.3: return ("Hostd",2098101,"info","local4",pick([f"[Originator@6876 sub=Vimsvc.TaskManager opID={random.getrandbits(32):08x}] Task Created : haTask-{random.randint(1,400)}-vim.VirtualMachine.{pick(['createSnapshot','reconfigure','powerOn','removeSnapshot'])}-{random.randint(100000000,999999999)}",f"[Originator@6876 sub=Vmsvc.vm:/vmfs/volumes/vsan:{random.getrandbits(64):016x}/vm-{random.randint(10,80)}/vm.vmx] State Transition (VM_STATE_ON -> VM_STATE_CREATE_SNAPSHOT)",f"[Originator@6876 sub=Default opID={random.getrandbits(32):08x}] Accepted password for user vpxuser from 127.0.0.1",f"[Originator@6876 sub=Libs opID={random.getrandbits(32):08x}] NetstackInstanceImpl: congestion control algorithm: newreno"]))
    if x<0.5: return ("vmkernel",None,"info","kern",pick([f"cpu{random.randint(0,31)}:{random.randint(2097000,2099999)})ScsiDeviceIO: 4176: Cmd(0x45b9c1e2d8c0) 0x89, CmdSN 0x{random.getrandbits(24):x} from world {random.randint(2097000,2099999)} to dev \"naa.6000c29{random.getrandbits(40):010x}\" failed H:0x0 D:0x2 P:0x0 Valid sense data: 0x5 0x24 0x0.",f"cpu{random.randint(0,31)}:{random.randint(2097000,2099999)})NMP: nmp_ThrottleLogForDevice:3802: Cmd 0x28 (0x45b9c1e2d8c0, {random.randint(2097000,2099999)}) to dev \"naa.6000c29{random.getrandbits(40):010x}\" on path \"vmhba0:C0:T{random.randint(0,3)}:L0\" Failed:",f"cpu{random.randint(0,31)}:{random.randint(2097000,2099999)})VSAN: Vsan{pick(['Dom','Lsom','Cmmds'])}: {pick(['Owner for object changed','Component state updated to ACTIVE','Resync completed for object'])} {random.getrandbits(64):016x}",f"cpu{random.randint(0,31)}:{random.randint(2097000,2099999)})WARNING: vmw_ahci[0000:00:1f.2]: ahciAbortIO:(curr) HWQD: {random.randint(0,31)} BusyL: 0",f"cpu{random.randint(0,31)}:{random.randint(2097000,2099999)})Tcpip_Vmk: 1223: {pick(['vmk0','vmk1','vmk2'])}: link down -> up"]))
    if x<0.65: return ("Vpxa",2098200,"info","local4",f"[Originator@6876 sub=vpxLro opID={random.getrandbits(32):08x}] [VpxLRO] -- {pick(['BEGIN','FINISH'])} lro-{random.randint(100000,999999)} -- vpxa -- vpxapi.VpxaService.{pick(['fetchQuickStats','retrieveChanges','reserveManagedBy'])}")
    if x<0.8: return ("Rhttpproxy",2098050,"info","local4",f"[Originator@6876 sub=Proxy Req {random.randint(10000,99999)}] {pick(['New proxy client SSL(TCP(local=10.16.5.%d:443, peer=10.16.5.2:%d))'%(random.randint(11,14),random.randint(30000,65000)),'Resolved endpoint : [N7Vmacore4Http16LocalServiceSpecE:0x000000a6c1f3b2c0] _serverNamespace = /sdk action = Allow _port = 8307'])}")
    if x<0.9: return ("vobd",None,"notice","local0",pick(["[vmfsCorrelator] 2098120us: [esx.problem.vmfs.heartbeat.timedout] Lost access to volume vsanDatastore","[netCorrelator] [esx.clear.net.vmnic.linkstate.up] Physical NIC vmnic1 linkstate is up.","[scsiCorrelator] [esx.problem.scsi.device.io.latency.high] Device naa.6000c29 performance has deteriorated. I/O latency increased from average value of 1824 microseconds to 41235 microseconds."]))
    return ("crond",2097600,"info","cron",f"USER root pid {random.randint(2097000,2099999)} cmd /usr/lib/vmware/vmksummary/log-heartbeat.py")

def firewall(h):
    x=r()
    if x<0.6: return ("kernel",None,"info","kern",f"[LAN_LOCAL-{pick(['A','D'])}-{random.randint(2000,2100)}] IN=br{random.randint(10,20)} OUT=eth{random.randint(8,9)} MAC=... SRC={pick(IPS)} DST={random.randint(1,223)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)} LEN={random.randint(40,1500)} TOS=0x00 PREC=0x00 TTL=63 ID={random.randint(0,65535)} PROTO={pick(['TCP','TCP','UDP'])} SPT={random.randint(1024,65000)} DPT={pick([443,443,80,53,123,22,8443])}")
    if x<0.8: return ("hostapd",random.randint(1000,2000),"info","daemon",f"ath{random.randint(0,3)}: STA {random.getrandbits(48):012x} IEEE 802.11: {pick(['associated','disassociated','authenticated'])}")
    if x<0.9: return ("dnsmasq-dhcp",random.randint(1000,2000),"info","daemon",f"DHCPACK(br10) 10.20.1.{random.randint(10,60)} {random.getrandbits(48):012x} {pick(['xps9530','ws3','ss8510','printer-01','iphone'])}")
    return ("openvpn",random.randint(1000,2000),"notice","daemon",f"{pick(USERS)}/{pick(IPS)}:{random.randint(30000,65000)} {pick(['TLS: Initial packet','Control Channel: TLSv1.3, cipher TLSv1.3 TLS_AES_256_GCM_SHA384','SIGTERM[soft,remote-exit] received, client-instance exiting'])}")

def switch(h):
    x=r()
    if x<0.5: return ("%LINK-3-UPDOWN",None,"err" if r()<0.5 else "notice","local0",f"Interface GigabitEthernet1/0/{random.randint(1,48)}, changed state to {pick(['up','down'])}")
    if x<0.7: return ("%LINEPROTO-5-UPDOWN",None,"notice","local0",f"Line protocol on Interface GigabitEthernet1/0/{random.randint(1,48)}, changed state to {pick(['up','down'])}")
    if x<0.85: return ("%SYS-5-CONFIG_I",None,"notice","local0",f"Configured from console by {pick(USERS)} on vty{random.randint(0,4)} ({pick(IPS)})")
    if x<0.95: return ("%SEC_LOGIN-5-LOGIN_SUCCESS",None,"notice","local0",f"Login Success [user: {pick(USERS)}] [Source: {pick(IPS)}] [localport: 22]")
    return ("%SPANTREE-2-ROOTGUARD_BLOCK",None,"crit","local0",f"Root guard blocking port GigabitEthernet1/0/{random.randint(1,48)} on VLAN00{random.randint(10,20)}.")

def nas(h):
    x=r()
    if x<0.3: return ("smbd",random.randint(10000,20000),"info","daemon",f"{pick(USERS)} connected to share {pick(['workstation-holding','backups','media','vendor'])} from {pick(IPS)}")
    if x<0.5: return ("synoscgi",random.randint(20000,30000),"info","daemon",pick(["Snapshot [GMT-06-2026.{:02d}.{:02d}-{:02d}.05.01] of share [workstation-holding] taken by [storage]".format(random.randint(5,11),random.randint(1,28),random.randint(0,23)),"User [alice] logged in from [10.16.3.13] via [DSM]","Retention policy applied to share [workstation-holding]: 2 snapshots removed"]))
    if x<0.7: return ("kernel",None,"info","kern",pick([f"md: md2: data-check done.",f"ata{random.randint(1,8)}: SATA link up 6.0 Gbps (SStatus 133 SControl 300)",f"EXT4-fs (dm-1): mounted filesystem with ordered data mode. Opts: (null)",f"nfsd: last server has exited, flushing export cache"]))
    if x<0.85: return ("rsyncd",random.randint(10000,20000),"info","daemon",f"sent {random.randint(1000,900000000)} bytes received {random.randint(100,90000)} bytes total size {random.randint(1000000,9000000000)}")
    return ("smartd",random.randint(3000,4000),"warning","daemon",f"Device: /dev/sd{pick('abcdefgh')} [SAT], SMART Usage Attribute: 194 Temperature_Celsius changed from {random.randint(30,40)} to {random.randint(36,48)}")

GEN={"web":web,"app":app,"db":db,"esxi":esxi,"firewall":firewall,"switch":switch,"nas":nas}
WEIGHT={"web":1.6,"app":1.4,"db":1.0,"esxi":1.3,"firewall":1.8,"switch":0.25,"nas":0.5}
MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

def incidents(start, days, rng):
    """(start_ts, end_ts, host_index, kind) scattered over the window."""
    out=[]
    kinds=["ssh_bruteforce","disk_full","oom","link_flap","db_deadlocks","upstream_timeouts","vsan_latency","cert_expiry"]
    for i in range(14):
        d=rng.uniform(3,days-1); hour=rng.uniform(0,24)
        t0=start+dt.timedelta(days=d,hours=hour); dur=dt.timedelta(minutes=rng.uniform(8,120))
        k=kinds[i%len(kinds)]; ok={"ssh_bruteforce":("web","app","db","nas"),"disk_full":("web","app","db","nas"),"oom":("web","app","db"),"link_flap":("switch","esxi","web"),"db_deadlocks":("db",),"upstream_timeouts":("web",),"vsan_latency":("esxi",),"cert_expiry":("web",)}[k]
        ix=rng.choice([i_ for i_,h in enumerate(HOSTS) if h[1] in ok])
        out.append((t0,t0+dur,ix,k))
    return out

def incident_event(kind,host):
    hn=host[0]
    if kind=="ssh_bruteforce": ip=pick(["45.142.212.%d"%random.randint(1,254),"185.220.101.%d"%random.randint(1,254)]); return ("sshd",random.randint(1000,65000),"warning","auth",f"Failed password for {pick(['root','admin','ubuntu','postgres','oracle'])} from {ip} port {random.randint(30000,65000)} ssh2")
    if kind=="disk_full": return (pick(["kernel","postgres","nginx","systemd"]),None,"err","daemon",pick([f"No space left on device: /var/lib/{pick(['postgresql/16/main/pg_wal','docker/overlay2','nginx/body'])}","write() failed (28: No space left on device) while writing to /var/log/nginx/access.log","PANIC:  could not write to file \"pg_wal/xlogtemp.1234\": No space left on device","Failed to write journal: No space left on device"]))
    if kind=="oom": return ("kernel",None,"err","kern",pick([f"Out of memory: Killed process {random.randint(1000,60000)} ({pick(['java','postgres','node','telegraf'])}) total-vm:{random.randint(1000000,9000000)}kB, anon-rss:{random.randint(500000,7000000)}kB, file-rss:0kB, shmem-rss:0kB",f"oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg=/system.slice/orders-api.service,task=java,pid={random.randint(1000,60000)},uid=1001","Memory cgroup out of memory: Killed process 2088 (java)"]))
    if kind=="link_flap": return ("%LINK-3-UPDOWN" if host[1]=="switch" else "kernel",None,"err","kern",f"Interface GigabitEthernet1/0/{random.randint(1,48)}, changed state to {pick(['up','down'])}" if host[1]=="switch" else f"ens192: NIC Link is {pick(['Down','Up 10 Gbps Full Duplex, Flow Control: None'])}")
    if kind=="db_deadlocks": return ("postgres",random.randint(3000,3900),"err","local0",f"[{random.randint(1,99999)}] ERROR:  deadlock detected\n\tDETAIL:  Process {random.randint(3000,3900)} waits for ShareLock on transaction {random.randint(100000,999999)}; blocked by process {random.randint(3000,3900)}.")
    if kind=="upstream_timeouts": return ("nginx",random.randint(1300,1400),"err","local0",f"*{random.randint(1,999)} upstream timed out (110: Connection timed out) while connecting to upstream, client: {pick(IPS)}, server: app.example.com, request: \"GET {pick(URLS)} HTTP/1.1\", upstream: \"http://10.20.2.{random.randint(10,12)}:8080{pick(URLS)}\"")
    if kind=="vsan_latency": return ("vobd",None,"warning","local0",f"[scsiCorrelator] [esx.problem.scsi.device.io.latency.high] Device naa.6000c29{random.getrandbits(40):010x} performance has deteriorated. I/O latency increased from average value of {random.randint(800,2500)} microseconds to {random.randint(30000,120000)} microseconds.")
    return ("nginx",random.randint(1300,1400),"err","local0",f"SSL_do_handshake() failed (SSL: error:0A000415:SSL routines::sslv3 alert certificate expired) while SSL handshaking, client: {pick(IPS)}, server: 0.0.0.0:443")

def rate_at(t, base_per_min):
    """events per minute across the fleet at UTC time t, business-hours + weekday curve + slow growth."""
    lh=(t.hour-6)%24  # local-ish (CST) hour
    diurnal=0.45+0.55*math.exp(-((lh-14)/5.0)**2)*1.6 if 7<=lh<=19 else 0.45+0.1*math.exp(-((lh-14)/6.0)**2)
    wk=0.55 if t.weekday()>=5 else 1.0
    return base_per_min*diurnal*wk

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--host",required=True); ap.add_argument("--user",default="admin")
    ap.add_argument("--days",type=int,default=184); ap.add_argument("--total",type=int,default=1_400_000)
    ap.add_argument("--agent-id",default="synth-syslog"); ap.add_argument("--seed",type=int,default=20261104)
    ap.add_argument("--end-offset-min",type=int,default=15); ap.add_argument("--dry-run",action="store_true")
    ap.add_argument("--sample",type=int,default=0,help="print N sample lines and exit")
    a=ap.parse_args(); random.seed(a.seed); rng=random.Random(a.seed+1)
    end=dt.datetime.now(dt.timezone.utc).replace(second=0,microsecond=0)-dt.timedelta(minutes=a.end_offset_min)
    start=end-dt.timedelta(days=a.days)
    # calibrate base rate so the curve integrates to --total
    minutes=a.days*1440; probe=sum(rate_at(start+dt.timedelta(minutes=m),1.0) for m in range(0,minutes,7))*7
    base=a.total/probe
    incs=incidents(start,a.days,rng)
    hosts_w=[WEIGHT[h[1]] for h in HOSTS]
    tok=None
    if not (a.dry_run or a.sample):
        r_=requests.post(f"https://{a.host}:9543/api/v2/sessions",json={"username":a.user,"password":os.environ["LI_PASS"],"provider":"Local"},verify=False,timeout=30); r_.raise_for_status(); tok=r_.json()["sessionId"]
    batch=[]; size=2; sent=0; t_begin=time.time(); last_report=t_begin
    def flush():
        nonlocal batch,size,sent,tok
        if not batch: return
        if not (a.dry_run or a.sample):
            body={"timestampValidation":False,"messages":[dict(e,id=i+1) for i,e in enumerate(batch)]}
            for attempt in range(8):
                try:
                    r_=requests.post(f"https://{a.host}:9543/api/v2/events/ingest/{a.agent_id}",json=body,headers={"Authorization":f"Bearer {tok}"},verify=False,timeout=180)
                except requests.RequestException as ex:
                    print("post error",ex,file=sys.stderr); time.sleep(10); continue
                if r_.status_code==503: time.sleep(int(r_.headers.get("Retry-After","5"))); continue
                if r_.status_code==401:
                    r2=requests.post(f"https://{a.host}:9543/api/v2/sessions",json={"username":a.user,"password":os.environ["LI_PASS"],"provider":"Local"},verify=False,timeout=30); r2.raise_for_status(); tok=r2.json()["sessionId"]; continue
                r_.raise_for_status(); break
            else: raise SystemExit("ingest kept failing")
        sent+=len(batch); batch=[]; size=2
    t=start; shown=0
    while t<end:
        lam=rate_at(t,base)*(0.9+0.2*random.random())
        n=max(0,int(random.gauss(lam,max(1,lam**0.5))))
        active=[(ix,k) for (t0,t1,ix,k) in incs if t0<=t<t1]
        for _ in range(n):
            if active and random.random()<0.6:
                ix,k=pick(active); h=HOSTS[ix]; appname,pid,sev,fac,msg=incident_event(k,h)
            else:
                h=random.choices(HOSTS,weights=hosts_w)[0]; appname,pid,sev,fac,msg=GEN[h[1]](h)
            ts=t+dt.timedelta(seconds=random.uniform(0,60))
            hdr=f"{MON[ts.month-1]} {ts.day:2d} {ts:%H:%M:%S} {h[0]} {appname}{'['+str(pid)+']' if pid else ''}: "
            fields=[{"name":"hostname","content":h[0]},{"name":"appname","content":appname},{"name":"severity","content":sev},{"name":"facility","content":fac},{"name":"syslog_pri","content":str(FAC[fac]*8+SEV[sev])},{"name":"site","content":h[2]},{"name":"environment","content":h[3]},{"name":"host_kind","content":h[1]},{"name":"dataset","content":"synth-syslog-6mo"}]
            if pid: fields.append({"name":"procid","content":str(pid)})
            if active and any(HOSTS[ix] is h for ix,_ in active): fields.append({"name":"incident","content":[k for ix,k in active if HOSTS[ix] is h][0]})
            e={"text":hdr+msg,"timestamp":int(ts.timestamp()*1000),"fields":fields}
            if a.sample:
                print(e["text"]); shown+=1
                if shown>=a.sample: return
            s=len(json.dumps(e))+1
            if size+s>MAX_POST_BYTES: flush()
            batch.append(e); size+=s
        t+=dt.timedelta(minutes=1)
        if time.time()-last_report>60:
            el=time.time()-t_begin; print(f"{dt.datetime.now():%H:%M:%S} at {t:%Y-%m-%d %H:%M}Z sent={sent} rate={sent/el*60:.0f}/min",flush=True); last_report=time.time()
    flush()
    print(f"DONE sent={sent} window {start:%Y-%m-%d}..{end:%Y-%m-%d %H:%M}Z incidents={[(t0.strftime('%m-%d %H:%M'),HOSTS[ix][0],k) for t0,t1,ix,k in incs]}",flush=True)
if __name__=="__main__": main()
