import java.lang.reflect.*;
import java.util.*;

/**
 * Register copied buckets into a NAMED index partition on an Operations for Logs node.
 * Same steps as `bucket-index add`, but through BucketIndex.addNewBucket(String partitionId, BucketId)
 * and setBucketStatus(BucketId, String partitionId, BucketStatus, Long). The service MUST be stopped.
 * usage: java -DLOGINSIGHT_HOME=/usr/lib/loginsight -cp <li>/application/lib/ingestion-service.jar:. \
 *          PartitionBucketAdd <partitionId|""> <status: archived|inactive> <creationTimeMillis|now> <bucketId>[,<bucketId>...]
 */
public class PartitionBucketAdd {
    public static void main(String[] a) throws Exception {
        if (a.length != 4) { System.err.println("usage: PartitionBucketAdd <partitionId> <status> <createTimeMillis|now> <ids,comma>"); System.exit(2); }
        String part = a[0];
        Class<?> console = Class.forName("com.vmware.loginsight.ingestion.BucketIndexConsole");
        Class<?> idxC = Class.forName("com.vmware.loginsight.ingestion.BucketIndex");
        Class<?> bidC = Class.forName("com.vmware.loginsight.commons.BucketId");
        Class<?> stC = Class.forName("com.vmware.loginsight.ingestion.BucketStatus");
        Method parseIds = console.getDeclaredMethod("parseBucketIds", String.class); parseIds.setAccessible(true);
        String store = System.getenv("PROJECTS_ROOT") + "/cidata/store/";
        if (System.getenv("PROJECTS_ROOT") == null) { System.err.println("PROJECTS_ROOT not set (source loginsight.conf)"); System.exit(2); }
        @SuppressWarnings("unchecked") List<Object> ids = (List<Object>) parseIds.invoke(null, a[3]);
        Object status = null;
        for (Object c : stC.getEnumConstants()) if (c.toString().equalsIgnoreCase(a[1])) status = c;
        if (status == null) { System.err.println("unknown status " + a[1] + " in " + Arrays.toString(stC.getEnumConstants())); System.exit(2); }
        long ct = a[2].equals("now") ? System.currentTimeMillis() : Long.parseLong(a[2]);
        Constructor<?> ctor = idxC.getConstructor(boolean.class);
        Object idx = ctor.newInstance(true);
        Method add = idxC.getMethod("addNewBucket", String.class, bidC);
        Method setSt = idxC.getMethod("setBucketStatus", bidC, String.class, stC, Long.class);
        Method info = idxC.getMethod("getBucketInfo", bidC);
        for (Object id : ids) {
            Object existing = null;
            try { existing = info.invoke(idx, id); } catch (InvocationTargetException e) { existing = null; }
            String uuid = (String) bidC.getMethod("getUuid").invoke(id);
            java.nio.file.Path dir = java.nio.file.Paths.get(store + uuid);
            if (!java.nio.file.Files.isDirectory(dir)) { System.err.println("no bucket dir " + dir); System.exit(4); }
            long size;
            try (java.util.stream.Stream<java.nio.file.Path> w = java.nio.file.Files.walk(dir)) {
                size = w.filter(java.nio.file.Files::isRegularFile).mapToLong(f -> f.toFile().length()).sum(); }
            bidC.getMethod("setCreateTime", long.class).invoke(id, ct);
            if (existing != null) {
                setSt.invoke(idx, id, part, status, Long.valueOf(size));
                System.out.println("updated " + id + " partition=" + part + " status=" + status + " size=" + size); continue; }
            add.invoke(idx, part, id);
            setSt.invoke(idx, id, part, status, Long.valueOf(size));
            System.out.println("added " + id + " partition=" + (part.isEmpty() ? "<default>" : part) + " status=" + status + " size=" + size + " createTime=" + ct);
        }
        idxC.getMethod("commit").invoke(idx);
        idxC.getMethod("close").invoke(idx);
    }
}
