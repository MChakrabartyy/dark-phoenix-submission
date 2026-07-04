import { PrismaClient } from '@prisma/client';
const db = new PrismaClient();
const f = await db.uploadedFile.findUnique({ where: { id: 'cmr6smacj0001usik0isobs2r' }, select: { status: true } });
const clips = await db.clip.count({ where: { uploadedFileId: 'cmr6smacj0001usik0isobs2r' } });
console.log(f.status, 'clips:', clips);
await db.$disconnect();
