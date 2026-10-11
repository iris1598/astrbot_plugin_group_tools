# astrbot_plugin_group_tools

把 OneBot v11（aiocqhttp / NapCat / Lagrange 等）的群管理接口封装成 **AstrBot 的 LLM 工具**，
并支持 **分群启用功能**：每个群可以单独配置允许大模型使用哪些能力。

此外，语音消息**不会**注册成 LLM 工具，而是在请求大模型前自动转成文字注入上下文
（做法参考 [astrbot_plugin_mimo_media](https://github.com/iris1598/astrbot_plugin_mimo_media) 的 `llonebot_stt` 模式）。

## 功能一览

共 **32 个 LLM 工具**，按 10 个功能类别分组，每类都可以在 WebUI 里按群开关；
其中「语音自动转文字」是通过 `on_llm_request` 钩子实现的，不是工具。

| 功能类别 | key | 工具（LLM 工具名） | 对应 OneBot action |
| --- | --- | --- | --- |
| 群名片与头衔 | `card` | `onebot_set_group_card`<br>`onebot_set_group_special_title` | `set_group_card`<br>`set_group_special_title` |
| 禁言管理 | `mute` | `onebot_set_group_ban`<br>`onebot_set_group_whole_ban`<br>`onebot_get_group_shut_list` | `set_group_ban`<br>`set_group_whole_ban`<br>`get_group_shut_list` |
| 群精华消息 | `essence` | `onebot_get_essence_msgs`<br>`onebot_set_essence_msg`<br>`onebot_delete_essence_msg` | `get_essence_msg_list`<br>`set_essence_msg`<br>`delete_essence_msg` |
| 群公告 | `notice` | `onebot_send_group_notice`<br>`onebot_get_group_notice`<br>`onebot_delete_group_notice` | `_send_group_notice`<br>`_get_group_notice`<br>`_delete_group_notice` |
| 群相册 | `album` | `onebot_create_group_album`<br>`onebot_get_group_album_list`<br>`onebot_delete_group_album`<br>`onebot_upload_group_album`<br>`onebot_get_group_album_media` | `create_group_album`<br>`get_group_album_list`<br>`delete_group_album`<br>`upload_group_album`<br>`get_group_album_media_list` |
| 群头像 | `portrait` | `onebot_set_group_portrait` | `set_group_portrait` |
| 互动 | `interact` | `onebot_set_msg_emoji_like`<br>`onebot_send_poke` | `set_msg_emoji_like`<br>`send_poke` |
| 群文件 | `file` | `onebot_upload_group_file`<br>`onebot_set_group_file_forever`<br>`onebot_delete_group_file`<br>`onebot_move_group_file`<br>`onebot_create_group_file_folder`<br>`onebot_delete_group_file_folder`<br>`onebot_get_group_file_system_info`<br>`onebot_get_group_root_files`<br>`onebot_get_group_files_by_folder`<br>`onebot_rename_group_file_folder`<br>`onebot_rename_group_file` | `upload_group_file`<br>`set_group_file_forever`<br>`delete_group_file`<br>`move_group_file`<br>`create_group_file_folder`<br>`delete_group_folder`<br>`get_group_file_system_info`<br>`get_group_root_files`<br>`get_group_files_by_folder`<br>`rename_group_file_folder`<br>`rename_group_file` |
| 语音自动转文字 | `stt` | ——（`on_llm_request` 钩子自动注入，无工具） | `voice_msg_to_text` |
| 机器人自身 | `self` | `onebot_set_self_group_card`<br>`onebot_set_qq_avatar` | `set_group_card`（`user_id` 自动填机器人自己）<br>`set_qq_avatar` |

> 接口参数以 NapCat / OneBot v11 的字段名为准，例如 `set_group_special_title` 用 `special_title`，
> `create_group_file_folder` 用 `name`，`delete_group_folder` 对应工具 `onebot_delete_group_file_folder`。

> 「机器人自身」这一类的两个工具都不需要传 QQ 号：
> `onebot_set_self_group_card` 自动把 `user_id` 填成机器人自己（改的是它在**当前群**的名片），
> `onebot_set_qq_avatar` 改的是机器人账号的**个人头像**（不是群头像，群头像用 `onebot_set_group_portrait`）。

## 安装

```bash
cd AstrBot/data/plugins
git clone <本插件仓库地址> astrbot_plugin_group_tools
```

或者在 WebUI 的「插件」页通过仓库地址安装。无需额外依赖（只用 AstrBot 自带能力）。

## 配置

在 WebUI 的「插件 → OneBot 群管理工具 → 配置」中设置：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enable_llm_tools` | `true` | 是否把这 32 个工具注册给大模型。关闭后只保留 `/群管` 指令。修改后需要重载插件。 |
| `enable_auto_stt` | `true` | 语音自动转文字的全局总开关。关闭后完全不做语音转写。 |
| `write_tool_permission` | `admin` | 会改动内容的工具（禁言、改名、发公告、改文件、改机器人资料等）默认需要的权限。`admin` = 仅 AstrBot 管理员；`member` = 所有群成员。 |
| `readonly_tool_permission` | `admin` | 只读查询类工具（群成员、群文件、群公告、群精华、相册列表等）默认需要的权限。默认 `admin`，即「除豁免项外全部需要管理员」。 |
| `member_tools` | 见下 | **免管理员权限的工具**白名单，优先级高于上面两项。默认放开「设置别人群名片」和「禁言」相关工具。 |
| `unconfigured_group_policy` | `allow` | 没有在下面列表里登记的群：`allow` 默认全开，`deny` 默认全关。 |
| `groups` | `[]` | **分群功能开关**，每个条目包含群号、插件总开关和 10 个功能类别开关。 |

`member_tools` 的默认值：

```json
[
  "onebot_set_group_card",
  "onebot_set_group_ban",
  "onebot_set_group_whole_ban",
  "onebot_get_group_shut_list"
]
```

### 分群功能开关示例

在 WebUI 里点「添加模板 → 群配置」，填写群号和开关即可，落盘后形如：

```json
{
  "groups": [
    {
      "__template_key": "group",
      "group_id": "123456789",
      "enabled": true,
      "card": true,
      "mute": true,
      "essence": true,
      "notice": true,
      "album": true,
      "portrait": true,
      "interact": true,
      "file": false,
      "stt": true,
      "self": true
    }
  ]
}
```

上面这个例子的含义是：群 `123456789` 启用本插件，但**不允许**大模型操作群文件，其余功能开放。
把 `self` 设为 `false`，就是禁止大模型改机器人在这个群的名片和它自己的 QQ 头像。

## 语音自动转文字

语音**不注册成 LLM 工具**，而是通过 `@filter.on_llm_request(priority=10)` 钩子，
在请求真正发给大模型之前自动完成转写并注入上下文，所以模型不需要「想到去调用工具」：

1. 扫描当前消息链，找出所有 `Record`（语音）消息段，并递归展开回复引用（`Reply.chain`）
   与合并转发（`Node` / `Nodes`）——合并转发里的语音会被跳过，因为拿不到可靠的 `message_id`。
2. 对每条语音取 `message_id`：回复语音时用被引用消息的 ID，直接发语音时用当前消息的 ID；
   按 ID 去重后调用协议端 `voice_msg_to_text`。
3. 注入结果：
   - **回复一条语音**：把被引用消息块里的 `[Empty Text]` 替换成转写文本，
     形如 `<Quoted Message>\n(小明): 今天下午三点开会\n</Quoted Message>`；
   - **直接发语音**：在请求里追加一段转写文本；
   - 同时清掉 AstrBot 生成的 `[Audio Attachment: path ...]` 占位符并清空 `audio_urls`，
     避免把原始语音文件再塞给模型；
   - 全部转写失败时追加 `[语音转文字失败，已跳过]`，并保留原始音频作为回退。

按群关闭：在分群开关里把 `stt` 设为关即可；全局关闭用 `enable_auto_stt`。
私聊没有分群概念，只受 `enable_auto_stt` 控制。

## 指令

所有指令都要求 AstrBot 管理员权限（`filter.PermissionType.ADMIN`，即主配置 `admins_id` 中的 QQ）。

| 指令 | 说明 |
| --- | --- |
| `/群管 状态` | 查看当前群各功能类别的启用状态 |
| `/群管 开启 <功能>` | 开启当前群的某个功能，如 `/群管 开启 群文件` |
| `/群管 关闭 <功能>` | 关闭当前群的某个功能 |
| `/群管 开启全部` | 开启当前群的全部功能 |
| `/群管 关闭全部` | 关闭当前群的全部功能 |
| `/群管 功能` | 列出可用的功能类别与 key |

指令组别名：`/群管理`、`/grouptools`；功能名既可以用 key（`file`），也可以用中文（`群文件`、`文件`）。
指令的改动会立即写入配置文件，WebUI 刷新后可以看到。

## 权限与安全设计

1. **只作用于当前群**：所有工具都不接受 LLM 传入的 `group_id`，群号一律取当前会话所在群。
   这样可以避免提示词注入诱导模型去操作别的群。
2. **默认全部需要管理员，只放开两项**：`write_tool_permission` 与 `readonly_tool_permission`
   默认都是 `admin`，也就是说**除 `member_tools` 白名单外的 28 个工具都只允许 AstrBot 管理员调用**
   （含只读查询类）。默认白名单是：

   | 工具 | 说明 |
   | --- | --- |
   | `onebot_set_group_card` | 设置**别人**的群名片 |
   | `onebot_set_group_ban` | 群禁言 |
   | `onebot_set_group_whole_ban` | 群全体禁言 |
   | `onebot_get_group_shut_list` | 获取被禁言群员列表 |

   注意：`onebot_set_self_group_card`（改机器人**自己**的群名片）和 `onebot_set_qq_avatar`
   （改机器人头像）**不在**白名单里，仍然需要管理员。

   调整方式：
   - 想让普通成员也能查询群信息 → `readonly_tool_permission` 改成 `member`；
   - 想让普通成员也能执行全部写操作 → `write_tool_permission` 改成 `member`；
   - 想单独放开某个工具 → 把工具名加进 `member_tools`。

   另外 AstrBot 自身还有一层「扩展 → 组件」里的单工具权限，插件工具默认是 `member`
   （即放行，由本插件自己判定）。两层是叠加的：在那边把某个工具设成 `admin` 会更严。
3. **分群开关在调用时校验**：功能被关掉后，工具会直接返回
   `error: 功能「群文件」未在当前群启用。`，不会调用协议端。
4. **多账号路由**：每次调用都会带上 `self_id`，多协议端实例不会串号。
5. **群成员昵称、消息内容等返回数据是不可信数据**，模型不应把它们当指令执行。

## 使用示例

配置好之后，在群里直接跟机器人说话即可，例如：

- 「把张三的群名片改成 摸鱼中」→ `onebot_set_group_card`
- 「把李四禁言 10 分钟」→ `onebot_set_group_ban(duration=600)`
- 「这条消息设为精华」→ `onebot_set_essence_msg`（默认作用于当前消息）
- 「看看群里有哪些文件」→ `onebot_get_group_root_files`
- 「把你自己在本群的名字改成 小助手」→ `onebot_set_self_group_card`（`user_id` 自动填机器人自己）
- 「换个头像」→ `onebot_set_qq_avatar`
- 回复一条语音后说「他刚才说了什么」→ 无需工具，转写文本已经自动注入上下文

## 注意事项

- 仅支持 **aiocqhttp（OneBot v11）** 平台，其他平台调用会返回明确错误；
  语音自动转文字同样只在 aiocqhttp 下生效。
- 语音转写依赖协议端的 `voice_msg_to_text`（NapCat / LLONE Bot 扩展接口），
  协议端不支持时会在日志里记录 `voice_msg_to_text 失败` 并回退为原始音频。
- 相册、群公告、群文件等接口是 NapCat / Lagrange 的扩展接口，
  不同协议端实现程度不同；如果协议端不支持，会返回 `error: 调用 xxx 失败：...`。
- `upload_group_file`、`set_group_portrait`、`upload_group_album`、`set_qq_avatar` 的路径参数是
  **协议端所在机器**上的路径，不是 AstrBot 容器内的路径；跨容器部署时请用 `http://` 或 `base64://`。
- `set_qq_avatar` 改的是机器人账号的**全局头像**，影响它在所有群和私聊里的形象，所以默认要求管理员；
  `set_group_card` 改自己名片时只影响当前群。
- 默认权限很严：普通群成员只能用「设置别人群名片」和「禁言」这几项，其余（包括查询类）都要管理员。
  如果被拦，返回的提示里会写明怎么放开。
- 需要机器人本身在群里具备相应权限（群主/管理员），否则协议端会返回权限错误。
- 列表类接口（群文件、相册、精华、公告）最多返回 50 条，避免撑爆模型上下文；
  相册媒体列表支持用 `attach_info` 翻页。

## 开发说明

- `main.py`：全部实现，约 1540 行。
- `_conf_schema.json`：插件配置 Schema，其中 `groups` 使用 `template_list`。
- 工具统一继承 `_GroupTool`，执行逻辑集中在 `_execute_tool()`：
  平台检查 → 分群开关检查 → 权限检查 → 组装参数（补 `group_id` / `self_id` / 默认 `message_id`）
  → 调用 `event.bot.api.call_action()` → 格式化结果。
- 语音转写走 `on_llm_request` 钩子，相关方法：`_collect_audio_targets()` →
  `_transcribe_audio()` → `_inject_transcriptions()`，消息链递归由模块级 `_walk_components()` 完成。
