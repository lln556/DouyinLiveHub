import os
import requests

# 创建保存图标的目录
save_dir = "data/level_img"
os.makedirs(save_dir, exist_ok=True)

# 等级范围 1~75
for level in range(1, 76):
    url = f"https://p11-webcast.douyinpic.com/img/webcast/new_user_grade_level_v1_{level}.png~tplv-obj.image"
    response = requests.get(url)
    
    if response.status_code == 200:
        file_path = os.path.join(save_dir, f"level_{level}.png")
        with open(file_path, "wb") as f:
            f.write(response.content)
        print(f"✅ 已保存等级 {level} 图标 -> {file_path}")
    else:
        print(f"❌ 下载失败：等级 {level}，状态码 {response.status_code}")
