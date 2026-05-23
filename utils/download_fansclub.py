import os
import requests

# 创建保存图标的目录
save_dir = "data/fansclub_img"
os.makedirs(save_dir, exist_ok=True)

# 粉丝团等级范围 1~20
for level in range(1, 21):
    url = f"https://p11-webcast.douyinpic.com/img/webcast/fansclub_new_advanced_badge_{level}_xmp.png~tplv-obj.image"
    response = requests.get(url)

    if response.status_code == 200:
        file_path = os.path.join(save_dir, f"fansclub_{level}.png")
        with open(file_path, "wb") as f:
            f.write(response.content)
        print(f"已保存粉丝团等级 {level} 图标 -> {file_path}")
    else:
        print(f"下载失败：粉丝团等级 {level}，状态码 {response.status_code}")
