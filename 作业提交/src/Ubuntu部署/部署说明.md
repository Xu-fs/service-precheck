# 阿里云 Ubuntu 公网教学部署说明

目标实例为用户提供的阿里云 ECS，公网 IP `47.117.178.247`，Ubuntu 24.04，Python 3.12.3。用户2026年11月8日提供的终端截图显示可用内存约1.3 GiB、磁盘剩余约35 GB、80端口未被现有服务占用。截图不等于安装已执行或公网已上线。

## 本次交付与验证边界

部署包为`作业提交/src/Ubuntu部署/zhengwu-mvp-ubuntu.tar.gz`，不含本机虚拟环境、凭证、数据库或Git历史。仅部署虚构业务工作流，模型服务配置留空。已验证Python初始化测试两项、Bash语法和压缩包路径安全；服务器实际安装、安全组放行及公网访问仍须在Workbench完成，尚不能标记上线。

使用[wizard技能](C:/Users/26381/.codex/skills/wizard/SKILL.md)的模板生成终端向导，只替换模板STAGES之后的过程。三个阶段为独立应用安装、安全组放行、浏览器外网验证。该向导需要用户在已登录的Workbench运行；没有建立新的SSH访问密钥。

## 上传与执行

在Workbench左侧文件管理中进入`/root`，上传`zhengwu-mvp-ubuntu.tar.gz`。如果网页文件管理没有上传入口，可使用Workbench终端工具栏的文件上传功能；以实际界面为准。

在服务器终端执行：

```bash
tar -xzf /root/zhengwu-mvp-ubuntu.tar.gz -C /root
bash /root/zhengwu-deploy-20261108/scripts/deploy_ubuntu_wizard.sh
```

默认目标为本实例IP。开始向导后按Enter继续，安装会从Ubuntu软件源及Python包索引下载依赖；没有改用不明下载源或执行远程未知脚本。不要把旧演示数据库上传到服务器。

## 服务器文件归属

| 服务器位置 | 用途 |
| --- | --- |
| /opt/zhengwu-mvp/app | 独立源码副本 |
| /opt/zhengwu-mvp/venv | 服务器依赖环境 |
| /var/lib/zhengwu-mvp | 私有虚构运行数据和演示登录资料 |
| /etc/zhengwu-mvp/mvp.env | 随机签名密钥及本机服务设置，权限0600 |
| /etc/systemd/system/zhengwu-mvp.service | 以独立非登录账号运行的后台服务 |
| /etc/nginx/conf.d/zhengwu-mvp.conf | 仅新增本网站代理配置，保留其他网站 |

遇到同名外来目录、运行资料、服务文件或Nginx配置时停止，不覆盖。初始化仅更新本工具归属的虚构账号，首次生成随机演示口令；重复执行保留登录资料和工单状态。账号文件使用`sudo cat /var/lib/zhengwu-mvp/demo-access.txt`在服务器查看，不写入项目或提交文档。

## 公网入口与验收

应用仅监听`127.0.0.1:18080`，Nginx接收80端口。安全组仅需新增本网站入方向TCP 80，不关闭整个防火墙，不改动原有SSH或其他规则。安装脚本仅在UFW原本已启用时添加80规则。

在本机浏览器验证以下预期入口，验证前不要标记为已上线：

- `http://47.117.178.247/health`返回本地应用教学范围的ok状态。
- `http://47.117.178.247/examples/workflow`使用demo_citizen登录，两个预设案例能够逐步及自动流转。
- `http://47.117.178.247/staff/login`分别登录窗口、审核员、主任，确认岗位入口及工单关系。
- 用户确认结案后刷新仍保存；手机宽度可读。

该初始入口为HTTP，仅供虚构课程演示。真实资料和正式使用需要配置用户选择的域名及可信HTTPS，开启安全Cookie，再做正式环境验收。不要将此次教学安装称为生产就绪。

## 状态和排错

```bash
systemctl status zhengwu-mvp --no-pager
journalctl -u zhengwu-mvp -n 60 --no-pager
nginx -t
curl -H 'Host: 47.117.178.247' http://127.0.0.1/health
```

如果依赖安装、服务健康检查或Nginx校验失败，保留输出定位原因，不关闭权限或反复清空目录。端口、目录、密钥、实际安装结果及公网验收要分别核对。

设计参考：[FastAPI部署概念](https://fastapi.tiangolo.com/deployment/concepts/)，说明HTTPS、开机启动、重启及进程内存等部署问题。采用单worker以控制2 GiB教学主机的内存需求；实际资源消耗仍须服务器运行后观察。
