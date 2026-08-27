# Odd Card Detector

โปรแกรมเปรียบเทียบการ์ด 6 ใบและคลิกใบที่ต่างจากกลุ่มส่วนใหญ่โดยอัตโนมัติ

## ติดตั้ง

```powershell
python -m pip install -r requirements.txt
```

## ตั้งกรอบครั้งแรก

เปิด LDPlayer ให้เห็นหน้าต่างมินิเกม แล้วรัน:

```powershell
python detector.py --calibrate
```

หากใช้จอที่สอง ให้ตรวจหมายเลขจอและ calibrate บนจอ 2:

```powershell
python detector.py --list-monitors
python detector.py --calibrate --monitor 2
```

หมายเลขจอจะถูกบันทึกใน `config.json` จากนั้นรัน `python detector.py` ได้ตามปกติ โปรแกรมจะชดเชยตำแหน่งจอสำหรับการคลิกเมาส์ให้อัตโนมัติ

## ใช้ ADB โดยไม่ขยับเมาส์

เปิด ADB debugging ใน LDPlayer แล้วตรวจ device:

```powershell
python detector.py --list-adb-devices
```

จากนั้น calibrate ใหม่จากภาพ Android โดยตรง:

```powershell
python detector.py --calibrate --backend adb
```

ถ้ามีหลาย LDPlayer instances ให้เลือก serial:

```powershell
python detector.py --calibrate --backend adb --adb-device 127.0.0.1:5555
```

หลัง calibrate ให้รัน `python detector.py` ตามปกติ ค่า backend และ serial จะถูกอ่านจาก `config.json` โหมด ADB ใช้ `screencap` และ `input tap` ภายใน emulator จึงไม่ขยับเมาส์ Windows และไม่ขึ้นกับว่า LDPlayer อยู่จอใด หาก ADB เชื่อมไม่ได้ โปรแกรมจะหยุดและไม่ fallback ไปใช้เมาส์

## เล่น LDPlayer Macro ผ่าน ADB

อย่าเปิด playback จาก Operation Recorder พร้อมกัน ให้โปรแกรมเล่นไฟล์ `.record` แทน:

```powershell
python detector.py --macro-file "ด่าน 3.record"
```

ชื่อไฟล์แบบ relative จะอ่านจาก `C:\LDPlayer\LDPlayer14\vms\operationRecords` และวนซ้ำอัตโนมัติ หากต้องการเล่นรอบเดียวใช้ `--macro-once` โปรแกรมรองรับ touch, hold และ swipe; operation ประเภทอื่น เช่น clipboard จะถูกข้าม

พิกัดในไฟล์ `.record` ถูกเก็บเป็น `พิกเซล × 12` โปรแกรมจะหารด้วย 12 แล้วสเกลจาก `resolutionWidth/Height` ที่บันทึกไว้ไปยังความละเอียด ADB ปัจจุบัน จึงเล่นตำแหน่งเดียวกับ Operation Recorder แม้ความละเอียดเปลี่ยน

เมื่อพบการ์ด 5/6 ใบหรือ Confirm ตัวเล่น macro จะ pause timeline ทันที หลังฉากเหล่านี้หายต่อเนื่อง 0.5 วินาที จะ resume จาก operation และเวลาจุดเดิม โดยทุก touch ส่งผ่าน ADB และไม่ขยับเมาส์

ลากกรอบเฉพาะบริเวณรวมของการ์ดทั้ง 6 ช่องตามเส้นแดงในภาพ ตั้งแต่ขอบการ์ดซ้ายบนถึงขอบการ์ดขวาล่าง แล้วกด Enter หากย้ายหรือปรับขนาด LDPlayer ให้ตั้งกรอบใหม่

## ใช้งาน

เปิดหน้าต่างควบคุมสำหรับเลือก ADB device, macro และตัวเลือกต่าง ๆ:

```powershell
python detector.py --ui
```

ใน UI ใช้ `Refresh ADB` เมื่อต่อ emulator ใหม่ เลือกไฟล์ในช่อง Macro แล้วกด `Start`;
Console ด้านล่างจะแสดงสถานะตรวจจับและรายละเอียดการ reconnect โดยกด `Stop` เพื่อหยุดได้

หากคำสั่ง ADB timeout โปรแกรมจะลองใหม่สูงสุด 3 รอบ โดย reconnect device ก่อน และรีสตาร์ต
ADB server หากยังไม่สำเร็จ ข้อความจะแสดงชื่อคำสั่งที่มีปัญหาแทนการสรุปทันทีว่า ADB debugging ปิด

โปรแกรมตรวจตัวนับรูปแบบ `เลข/เลข` บริเวณด้านบนของเกมโดยเปรียบเทียบรูปร่างตัวเลขทั้งสองฝั่ง
จึงรองรับเลขใดก็ได้และไม่ขึ้นกับสีพื้นหลัง เมื่อสองฝั่งเหมือนกันจะกดตัวนับและเข้าสู่โหมดรอ Claim:

- ระหว่างรอจะบล็อก X, Cancel และ Confirm ทั้งหมด
- เมื่อพบปุ่ม Claim สีเขียว จะลองกดซ้ำได้สูงสุด 5 ครั้งจนปุ่มหาย
- หลัง Claim หายจึงคืนการตรวจปุ่มอื่นตามปกติ
- หากกดตัวนับแล้วไม่พบ Claim ภายใน 6 วินาที จะยกเลิกการบล็อกเพื่อไม่ให้ระบบค้างถาวร

ใน UI สามารถเปิดหรือปิดฟังก์ชันนี้ด้วยช่อง `ตรวจเลข/Claim` หรือปิดจาก command line ด้วย:

```powershell
python detector.py --no-counter
```

เปลี่ยนภาพต้นแบบ Claim ภายหลังได้ด้วย:

```powershell
python detector.py --claim-template "C:\path\to\claim.png"
```

หากพบ Cancel แล้วเกมไม่รับ tap ครั้งแรก โปรแกรมจะตรวจว่าปุ่มยังค้างอยู่และลองกดซ้ำทุก
0.65 วินาที สูงสุด 5 ครั้ง โดย Console จะแสดงเลขครั้งและพิกัดที่ส่ง ADB tap

ทดสอบโดยไม่คลิกจริง:

```powershell
python detector.py --dry-run
```

ใช้งานจริง:

```powershell
python detector.py
```

เมื่อพบการ์ด 6 ใบหรือจำนวนเปลี่ยนเหลือ 5 ใบ โปรแกรมจะเริ่มตรวจทันทีโดยไม่มี delay เริ่มต้น ช่องที่การ์ดหายจะไม่ถูกนำมาเปรียบเทียบ กด Q หรือเลื่อนเมาส์ไปมุมซ้ายบนสุดเพื่อหยุดฉุกเฉิน หากต้องการหน่วงเองยังระบุ `--delay วินาที` ได้

ก่อนคลิก การ์ดที่มีคะแนนสูงสุดจะถูกยืนยันจากเฟรมปัจจุบันเพียง 1 รอบ (`stable 1/1`) ระหว่างรอผลหลังคลิกจะหยุดตรวจจนกว่าการ์ดจะหายหรือครบเวลา 2 วินาที

โปรแกรมจะเลือกการ์ดที่มีคะแนนความแตกต่างสูงที่สุดจากการ์ดที่ยังอยู่ แสดงเป็นกรอบแดง และคลิกทันทีโดยไม่มีเกณฑ์คะแนนขั้นต่ำ

ตั้งภาพต้นแบบของปุ่ม `Confirm` หนึ่งครั้ง (ใช้ภาพที่ครอปเฉพาะปุ่ม):

```powershell
python detector.py --confirm-template "C:\path\to\confirm.png"
```

ตั้งภาพต้นแบบของปุ่ม `Cancel` (ไฟล์ค่าเริ่มต้นคือ `cancel_template.png`):

```powershell
python detector.py --cancel-template "C:\path\to\cancel.png"
```

ตั้งภาพต้นแบบปุ่มปิด `X`:

```powershell
python detector.py --close-template "C:\path\to\close.png"
```

ลำดับตรวจปุ่มคือ X, Cancel, Confirm และแต่ละปุ่มจะถูกกดเพียงครั้งเดียวต่อการปรากฏ
ปุ่ม X ใช้ mask ของเครื่องหมายสีขาวและเกณฑ์ 0.78 เพื่อรองรับวงกลม/ขนาดที่เปลี่ยน
หลังส่ง ADB tap หาก X ยังอยู่ โปรแกรมจะลองซ้ำทุก 0.7 วินาที สูงสุด 5 ครั้ง เพื่อรองรับ
ช่วง animation ที่ปุ่มมองเห็นแล้วแต่เกมยังไม่เปิดรับ input

ปุ่ม X จะถูกยกเว้นเมื่อพบหัวข้อ `Buy Upgrades!` หากหน้าตาเมนูเปลี่ยน สามารถเปลี่ยน
ภาพต้นแบบของหน้าต่างยกเว้นได้ด้วย:

```powershell
python detector.py --upgrade-window-template "C:\path\to\buy-upgrades.png"
```

ตั้งภาพต้นแบบปุ่ม action สีเขียว:

```powershell
python detector.py --action-template "C:\path\to\action.png"
```

หากต้องการรันโดยปิดเฉพาะการตรวจและกดปุ่ม action:

```powershell
python detector.py --no-action
```

ใช้ร่วมกับ macro ได้ เช่น `python detector.py --macro-file "ด่าน 3.record" --no-action`
โดยการตรวจการ์ด, X, Cancel และ Confirm ยังทำงานตามปกติ

ปุ่ม action จะถูกตรวจเฉพาะพื้นที่เล่นตรงกลางหน้าจอและกดทันทีรัว 10 ครั้งผ่านคำสั่ง ADB ชุดเดียว
โดยใช้เกณฑ์จับคู่ 0.45 พร้อมแสดงเปอร์เซ็นต์ที่ตรวจพบใน Console แต่จะไม่ตรวจหรือกดเมื่อพบ
หน้าต่าง `Buy Upgrades!` และปุ่มนี้จะไม่ pause macro

ตัวตรวจจะเทียบข้อความสีขาวกลางปุ่มแทนสีหรือทรงปุ่ม ตรวจ Cancel ก่อน Confirm
ทุกเฟรม และกด Cancel เพียงครั้งเดียวจนกว่าปุ่มจะหาย หากใช้ ADB การกดจะไม่ขยับเมาส์จริง

เมื่อเล่น ADB macro โปรแกรมจะสั่ง OBS ผ่าน WebSocket ให้อัดตั้งแต่เริ่มหรือ resume จนถึง
pause โดยให้ OBS ตั้งชื่อและเก็บไฟล์ใน Recording Path ตามปกติ หลังหยุดอัดโปรแกรมจะตรวจ
โฟลเดอร์นั้น เก็บวิดีโอใหม่สุด 3 คลิป และลบวิดีโอเก่ากว่านั้นแบบวนอัตโนมัติ

> หมายเหตุ: การอัดคลิป OBS ถูก comment ปิดไว้ชั่วคราวใน `run()` ระบบจะไม่เชื่อมต่อ
> เริ่ม/หยุดอัด หรือลบคลิป แต่ ADB macro และการ pause/resume ยังทำงานตามปกติ
ก่อนใช้งานให้เปิด OBS ด้วยมือ เปิด WebSocket Server และตั้ง Scene เป็น Game Capture หรือ
Window Capture ของ `dnplayer.exe` เมื่อพบเหตุ pause โปรแกรมจะหยุด macro ก่อน รอ 0.3 วินาที
แล้วสั่ง OBS หยุดอัดผ่าน API โดยไม่ส่งคีย์และไม่ดึงโฟกัส

เมื่อปุ่มที่ตรงกับภาพต้นแบบปรากฏใกล้บริเวณมินิเกม โปรแกรมจะคลิกทันทีตั้งแต่เฟรมแรก โปรแกรมจะไม่กดปุ่มสีเขียวอื่นที่ไม่ตรงกับคำว่า `Confirm`

เกณฑ์จับคู่ข้อความของปุ่ม Confirm และ Cancel คือ 0.75 ขึ้นไป และ Confirm ต้องมีพื้นสีเขียว

ตัวเลือกเพิ่มเติม:

```powershell
python detector.py --delay 0.7
```

ตัวตรวจปุ่มใช้ template ที่ cache ไว้ ตรวจบนภาพย่อ 50% และจำกัดพื้นที่ค้นหา โดยใช้ scale
ช่วงละ 0.05 เพื่อรองรับ animation/ขนาดคั่นกลาง benchmark ADB 1600x900 ใช้ประมาณ
0.48–0.54 วินาทีสำหรับ template matching ต่อรอบ

## ระบบเรียนรู้อัตโนมัติ

ระบบเรียนรู้เปิดอยู่เป็นค่าเริ่มต้น หลังคลิกจะรอผลสูงสุด 2 วินาที หากช่องที่คลิกหาย จะบันทึกเฉพาะใบนั้นเป็นตัวอย่าง odd ปุ่ม Confirm ทำงานแยกและไม่เกี่ยวข้องกับการยืนยันข้อมูลเรียนรู้ ระบบไม่ใช้ normal ในการตัดสินอีกต่อไป เพราะอาจมีท่าผิดปกติปนอยู่ ข้อมูล feature อยู่ใน `training_data/samples.npz` โหมด `--dry-run` จะไม่คลิกและไม่บันทึกตัวอย่าง

```powershell
python detector.py --learning-stats
python detector.py --no-learning
python detector.py --reset-learning
```

ระบบบันทึกเฉพาะ feature ลง `training_data/samples.npz` และไม่บันทึกไฟล์ภาพ PNG ระบบจะข้ามตัวอย่างใหม่หากซ้ำกับ odd เดิมมากเกินไป
