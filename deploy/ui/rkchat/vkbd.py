#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用内虚拟键盘（触摸屏用）。

为什么自己做：这台设备是 Goodix 触摸屏，但系统里没有任何软键盘
（onboard / maliit / squeekboard / florence 都没有），而且设备无网络
装不了包。GTK3 在 X11 下也不会自动弹键盘，所以点输入框没反应。

设计：
  - 不做输入法框架（XIM/ibus），直接把按键字符插进 Gtk.Entry。
    触摸场景够用，且不依赖任何系统组件。
  - 中文用「拼音 → 候选字」的简化方案：内置常用字表，
    输入拼音时在候选栏列出匹配汉字，点一下插入。无需联网、无需词库文件。
  - 布局跟随屏幕宽度自适应，按钮大小按可用空间算。

用法：
    kb = VirtualKeyboard(on_text=lambda s: entry.insert_text(s, -1),
                         on_backspace=..., on_enter=...)
    box.pack_start(kb.widget, False, False, 0)
    kb.show() / kb.hide()
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

# ── 内置中文候选字表 ────────────────────────────────────────────
# 只收高频字，按拼音首字母分组。触摸屏上打短句够用。
# 不追求完整词库：无网络、无词库文件，纯内置。
_PINYIN_TABLE: dict[str, str] = {
    "a": "啊阿爱安按暗案",
    "ai": "爱哎哀挨癌矮艾碍",
    "an": "安按暗案岸俺按时",
    "ba": "把八吧爸拔白百办半",
    "bai": "白百败拜摆",
    "ban": "办半版板班般搬伴",
    "bang": "帮棒榜绑磅",
    "bao": "包报保宝抱饱暴薄",
    "bei": "被北备背悲杯倍贝",
    "ben": "本苯奔笨",
    "bi": "比笔必毕币闭臂避",
    "bian": "变边便编遍扁",
    "biao": "表标彪膘",
    "bie": "别憋瘪",
    "bin": "宾滨彬斌",
    "bing": "并病冰兵饼",
    "bo": "波播博伯薄玻驳",
    "bu": "不步部布补捕簿",
    "cai": "才菜财采彩材猜",
    "can": "参餐残惨灿",
    "cao": "草操曹槽",
    "ce": "测策侧册",
    "ceng": "层曾蹭",
    "cha": "查茶差插察叉",
    "chang": "长常场厂唱尝肠",
    "chao": "超朝吵炒潮",
    "che": "车撤彻扯",
    "chen": "陈沉晨尘臣",
    "cheng": "成程称城承乘",
    "chi": "吃持尺迟池赤",
    "chong": "重冲充虫崇",
    "chou": "抽愁丑臭筹",
    "chu": "出处初除楚础储",
    "chuan": "传船穿川串",
    "chuang": "床创窗闯",
    "chui": "吹垂锤炊",
    "chun": "春纯唇醇",
    "ci": "次此词刺磁慈",
    "cong": "从聪丛葱",
    "cu": "粗促醋簇",
    "cun": "存村寸",
    "cuo": "错措挫搓",
    "da": "大打达答搭",
    "dai": "带代待戴袋贷",
    "dan": "但单担淡蛋弹胆",
    "dang": "当党挡档",
    "dao": "到道导倒刀盗",
    "de": "的地得德",
    "deng": "等灯登凳邓",
    "di": "地第低底弟敌递",
    "dian": "点电店典殿淀",
    "diao": "掉调吊雕",
    "die": "跌爹叠蝶",
    "ding": "定顶订丁钉",
    "diu": "丢",
    "dong": "动东懂冬洞冻",
    "dou": "都斗豆陡逗",
    "du": "读度独毒杜肚",
    "duan": "段短断端锻",
    "dui": "对队堆兑",
    "dun": "顿蹲盾敦",
    "duo": "多夺朵躲堕",
    "e": "额俄恶饿鹅",
    "en": "恩嗯",
    "er": "而二儿耳尔",
    "fa": "发法罚伐罚",
    "fan": "反饭翻犯范凡繁",
    "fang": "方放房防访芳",
    "fei": "非飞费肥废肺",
    "fen": "分份粉芬奋愤",
    "feng": "风丰封峰锋疯",
    "fo": "佛",
    "fou": "否",
    "fu": "复副服父福富负",
    "ga": "嘎",
    "gai": "改该盖概丐",
    "gan": "干感赶敢甘肝",
    "gang": "刚钢港岗纲",
    "gao": "高告搞稿糕",
    "ge": "个各格歌哥隔革",
    "gei": "给",
    "gen": "根跟",
    "geng": "更耕梗",
    "gong": "工公共攻功供",
    "gou": "够够沟狗购构",
    "gu": "古顾故股鼓骨谷",
    "gua": "挂瓜刮寡",
    "guai": "怪拐",
    "guan": "关管观惯馆官",
    "guang": "光广逛",
    "gui": "贵归规鬼桂柜",
    "gun": "滚棍",
    "guo": "过国果锅",
    "ha": "哈",
    "hai": "还海害孩",
    "han": "汉含寒喊汗",
    "hang": "行航杭",
    "hao": "好号毫浩豪",
    "he": "和合何河喝核",
    "hei": "黑嘿",
    "hen": "很恨狠痕",
    "heng": "横恒衡",
    "hong": "红洪宏虹",
    "hou": "后厚候猴",
    "hu": "和湖户护呼乎",
    "hua": "话化花华划画",
    "huai": "坏怀",
    "huan": "还换欢环缓",
    "huang": "黄荒慌皇",
    "hui": "会回灰挥汇",
    "hun": "混婚昏",
    "huo": "或活火货获",
    "ji": "机几及级即记计",
    "jia": "家加价假架佳",
    "jian": "见间件建简检",
    "jiang": "将讲江降奖",
    "jiao": "叫教交角脚焦",
    "jie": "接解界结街节",
    "jin": "进今金近尽仅",
    "jing": "经京精境竟静",
    "jiong": "穷",
    "jiu": "就九酒旧救久",
    "ju": "据局句具举居",
    "juan": "卷捐倦娟",
    "jue": "决觉绝角",
    "jun": "军均君",
    "ka": "卡咖",
    "kai": "开凯慨",
    "kan": "看刊砍勘",
    "kang": "抗康扛",
    "kao": "考靠烤",
    "ke": "可课克客科刻",
    "ken": "肯恳",
    "keng": "坑",
    "kong": "空控孔恐",
    "kou": "口扣寇",
    "ku": "苦哭库裤",
    "kua": "夸跨垮",
    "kuai": "快块筷",
    "kuan": "宽款",
    "kuang": "况矿狂框",
    "kui": "亏愧溃",
    "kun": "困昆捆",
    "kuo": "扩括阔",
    "la": "拉啦辣落",
    "lai": "来赖莱",
    "lan": "蓝烂兰篮懒",
    "lang": "浪狼郎",
    "lao": "老考劳牢",
    "le": "了乐勒",
    "lei": "类累雷泪",
    "leng": "冷愣",
    "li": "里力立理利离",
    "lian": "连联脸练恋",
    "liang": "两量亮凉良",
    "liao": "了料聊疗",
    "lie": "列烈裂劣",
    "lin": "林临邻淋",
    "ling": "另令领零灵",
    "liu": "六流留刘柳",
    "long": "龙隆笼聋",
    "lou": "楼漏搂",
    "lu": "路陆录露卢",
    "lv": "绿率旅律虑",
    "luan": "乱卵",
    "lue": "略掠",
    "lun": "论轮伦",
    "luo": "落罗络洛",
    "ma": "吗妈马码嘛",
    "mai": "买卖麦迈",
    "man": "满慢曼蛮",
    "mang": "忙盲茫芒",
    "mao": "毛贸冒帽猫",
    "me": "么",
    "mei": "没每美妹眉",
    "men": "们门闷",
    "meng": "猛梦盟蒙",
    "mi": "米密秘迷",
    "mian": "面免棉眠",
    "miao": "秒妙描苗",
    "mie": "灭",
    "min": "民敏",
    "ming": "名明命鸣",
    "miu": "谬",
    "mo": "莫模墨末磨",
    "mou": "某谋",
    "mu": "目木母幕牧",
    "na": "那拿哪纳",
    "nai": "奶耐乃",
    "nan": "难南男",
    "nang": "囊",
    "nao": "脑闹",
    "ne": "呢",
    "nei": "内",
    "nen": "嫩",
    "neng": "能",
    "ni": "你尼泥拟逆",
    "nian": "年念粘",
    "niang": "娘",
    "niao": "鸟尿",
    "nie": "捏",
    "nin": "您",
    "ning": "宁凝",
    "niu": "牛扭纽",
    "nong": "农弄浓",
    "nu": "努怒奴",
    "nv": "女",
    "nuan": "暖",
    "nue": "虐",
    "nuo": "诺挪",
    "o": "哦噢",
    "ou": "偶欧殴",
    "pa": "怕爬帕",
    "pai": "排牌拍派",
    "pan": "判盘盼",
    "pang": "旁胖",
    "pao": "跑泡炮抛",
    "pei": "配培陪佩",
    "pen": "盆喷",
    "peng": "朋碰鹏棚",
    "pi": "批皮匹疲",
    "pian": "片偏骗篇",
    "piao": "票漂飘",
    "pie": "撇",
    "pin": "品贫拼",
    "ping": "平评凭瓶",
    "po": "破迫颇坡",
    "pou": "剖",
    "pu": "普扑铺朴",
    "qi": "起其期气七器",
    "qia": "恰卡",
    "qian": "前钱千签浅",
    "qiang": "强墙抢枪",
    "qiao": "桥巧敲悄",
    "qie": "切且窃",
    "qin": "亲勤琴侵",
    "qing": "请情清轻青",
    "qiong": "穷",
    "qiu": "求球秋囚",
    "qu": "去取区曲趣",
    "quan": "全权圈劝",
    "que": "确却缺",
    "qun": "群裙",
    "ran": "然染燃",
    "rang": "让嚷",
    "rao": "绕扰",
    "re": "热",
    "ren": "人认任仁忍",
    "reng": "仍扔",
    "ri": "日",
    "rong": "容荣融溶",
    "rou": "肉柔",
    "ru": "如入乳",
    "ruan": "软",
    "rui": "锐瑞",
    "run": "润",
    "ruo": "若弱",
    "sa": "撒洒",
    "sai": "赛塞腮",
    "san": "三散伞",
    "sang": "桑丧嗓",
    "sao": "扫嫂",
    "se": "色涩",
    "sen": "森",
    "seng": "僧",
    "sha": "杀沙傻啥",
    "shai": "晒筛",
    "shan": "山善闪衫",
    "shang": "上商伤尚",
    "shao": "少烧稍勺",
    "she": "设社舍射",
    "shen": "什身深神甚",
    "sheng": "生声胜省升",
    "shi": "是时十事实使",
    "shou": "手受收首守",
    "shu": "书数术属输",
    "shua": "刷",
    "shuai": "帅摔衰",
    "shuan": "栓",
    "shuang": "双爽霜",
    "shui": "水谁睡税",
    "shun": "顺瞬",
    "shuo": "说硕",
    "si": "四死思司私",
    "song": "送松宋",
    "sou": "搜",
    "su": "素速诉苏宿",
    "suan": "算酸",
    "sui": "虽随岁碎",
    "sun": "孙损",
    "suo": "所索缩",
    "ta": "他她它塔踏",
    "tai": "太台态抬",
    "tan": "谈探叹弹坦",
    "tang": "堂唐糖躺",
    "tao": "套讨逃桃",
    "te": "特",
    "teng": "疼腾",
    "ti": "体题提替",
    "tian": "天田填甜",
    "tiao": "条跳调",
    "tie": "铁贴",
    "ting": "听庭停挺",
    "tong": "同通统痛",
    "tou": "头投透",
    "tu": "图土突途",
    "tuan": "团",
    "tui": "退推腿",
    "tun": "吞屯",
    "tuo": "脱托拖",
    "wa": "挖娃瓦",
    "wai": "外歪",
    "wan": "完万晚玩万",
    "wang": "王往望网忘",
    "wei": "为位未味委",
    "wen": "文问温闻稳",
    "weng": "翁",
    "wo": "我握窝卧",
    "wu": "无五物务武",
    "xi": "系西喜细习",
    "xia": "下夏吓峡",
    "xian": "先现线县显",
    "xiang": "想向相像象",
    "xiao": "小笑校效消",
    "xie": "些写谢协血",
    "xin": "新心信辛",
    "xing": "行性形星兴",
    "xiong": "兄胸凶",
    "xiu": "修休秀袖",
    "xu": "需许续须序",
    "xuan": "选宣旋悬",
    "xue": "学雪血",
    "xun": "寻迅训询",
    "ya": "压呀牙亚鸭",
    "yan": "眼言研严烟",
    "yang": "样阳养洋央",
    "yao": "要药摇咬腰",
    "ye": "也业夜叶页",
    "yi": "一以已意见",
    "yin": "因音引银印",
    "ying": "应英影硬营",
    "yo": "哟",
    "yong": "用永拥勇",
    "you": "有又由友右",
    "yu": "与于语育预",
    "yuan": "员元原远愿",
    "yue": "月越约阅",
    "yun": "运云允匀",
    "za": "杂咋砸",
    "zai": "在再栽载",
    "zan": "咱赞暂",
    "zang": "脏藏",
    "zao": "早造遭糟",
    "ze": "则责择",
    "zei": "贼",
    "zen": "怎",
    "zeng": "增曾",
    "zha": "扎炸眨",
    "zhai": "摘窄债",
    "zhan": "站占战展",
    "zhang": "张章长涨",
    "zhao": "找照招赵",
    "zhe": "这着者折",
    "zhen": "真针阵振",
    "zheng": "正整争证政",
    "zhi": "之只知指制",
    "zhong": "中种重终众",
    "zhou": "周州走轴",
    "zhu": "主助住注竹",
    "zhua": "抓",
    "zhuai": "拽",
    "zhuan": "转专砖",
    "zhuang": "装状庄壮",
    "zhui": "追坠",
    "zhun": "准",
    "zhuo": "桌捉浊",
    "zi": "子自字资紫",
    "zong": "总宗综纵",
    "zou": "走奏",
    "zu": "组足族阻",
    "zuan": "钻",
    "zui": "最嘴罪醉",
    "zun": "尊遵",
    "zuo": "做作坐左座",
}

# 高频短句，点一下直接上屏（触摸屏打长句太累）
PHRASE_HINTS = [
    "你好", "谢谢", "帮我看看", "这是什么", "介绍一下自己",
    "今天天气怎么样", "写一段话", "翻译成英文", "再说一次", "清空",
]


class VirtualKeyboard:
    """应用内触摸键盘。中英文都支持。"""

    ROWS_EN = [
        list("1234567890"),
        list("qwertyuiop"),
        list("asdfghjkl"),
        list("zxcvbnm,."),
    ]

    def __init__(self, on_text=None, on_backspace=None,
                 on_enter=None, on_hide=None):
        self.on_text = on_text or (lambda s: None)
        self.on_backspace = on_backspace or (lambda: None)
        self.on_enter = on_enter or (lambda: None)
        self.on_hide = on_hide or (lambda: None)
        # 默认中文拼音：中文输入需要“打拼音→选字”两段操作，
        # 默认英文时每次打中文都要先点一下切换；英文直接敲字母也快，
        # 且中英切换键就在底部，成本很低。
        self.cn_mode = True
        self.pinyin = ""
        self.compact = False       # 小屏时用更矮的按键
        self.widget = self._build()

    # ── 构建 ────────────────────────────────────────────────────
    def _build(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        root.get_style_context().add_class("vkbd")

        # 顶部行：候选栏 / 模式提示
        self.hint = Gtk.Label(label="")
        self.hint.set_xalign(0.0)
        self.hint.get_style_context().add_class("vkbd-hint")
        root.pack_start(self.hint, False, False, 2)

        # 候选词按钮区（中文模式用）
        self.cand_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                spacing=4)
        root.pack_start(self.cand_box, False, False, 0)

        # 键盘主体
        self.keys_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        root.pack_start(self.keys_box, False, False, 0)
        self._fill_keys()
        return root

    def _fill_keys(self):
        for ch in self.keys_box.get_children():
            self.keys_box.remove(ch)

        if self.cn_mode:
            # 中文模式：字母 + 数字（打拼音），底部功能键
            rows = [list("1234567890"), list("qwertyuiop"),
                    list("asdfghjkl"), list("zxcvbnm")]
            extra = [("删除", self._backspace), ("空格", lambda: self._emit(" ")),
                     ("拼音上屏", self._commit_pinyin),
                     ("英文", self._toggle_cn), ("收起", self._hide)]
        else:
            rows = self.ROWS_EN
            extra = [("删除", self._backspace), ("空格", lambda: self._emit(" ")),
                     ("中文", self._toggle_cn), ("收起", self._hide)]

        for row in rows:
            r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
            for ch in row:
                b = self._key_button(ch, lambda c=ch: self._emit(c))
                r.pack_start(b, True, True, 0)
            self.keys_box.pack_start(r, False, False, 0)

        # 底部功能行
        fr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        for label, cb in extra:
            b = Gtk.Button(label=label)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.get_style_context().add_class("vkbd-fn")
            if self.compact:
                b.get_style_context().add_class("vkbd-compact")
            b.connect("clicked", lambda _w, f=cb: (f(), False)[1])
            fr.pack_start(b, True, True, 0)
        enter = Gtk.Button(label="发送")
        enter.set_relief(Gtk.ReliefStyle.NONE)
        enter.get_style_context().add_class("vkbd-enter")
        if self.compact:
            enter.get_style_context().add_class("vkbd-compact")
        enter.connect("clicked", lambda _w: (self._enter(), False)[1])
        fr.pack_start(enter, True, True, 0)
        self.keys_box.pack_start(fr, False, False, 0)
        self.keys_box.show_all()
        self.refresh_hint()

    def _key_button(self, ch: str, cb) -> Gtk.Button:
        b = Gtk.Button(label=ch)
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.get_style_context().add_class("vkbd-key")
        if self.compact:
            b.get_style_context().add_class("vkbd-compact")
        # 触摸屏点击面积要大，同时避免焦点被键盘抢走导致输入框失焦
        b.set_can_focus(False)
        b.connect("clicked", lambda _w: (cb(), False)[1])
        return b

    # ── 行为 ────────────────────────────────────────────────────
    def _emit(self, ch: str):
        if self.cn_mode:
            # 中文模式：字母累积成拼音，数字/符号直接上屏
            if ch.isalpha() and ch.isascii():
                self.pinyin += ch
                self.refresh_hint()
                return
            self._commit_pinyin()
            self.on_text(ch)
            return
        self.on_text(ch)

    def _backspace(self):
        if self.cn_mode and self.pinyin:
            self.pinyin = self.pinyin[:-1]
            self.refresh_hint()
            return
        self.on_backspace()

    def _candidates(self) -> list[str]:
        """给当前拼音串出候选字。

        表是单字表（没有 nihao/zhongguo 这种词组），所以：
          1 整串精确命中 -> 直接给该音的字
          2 否则取「最长的有效前缀」的字（如 nihao -> ni 的「你」）
          这样输入法式的连续输入也能出字，不会因为多打几个字母就空候选。
        """
        s = self.pinyin
        if not s:
            return []
        if s in _PINYIN_TABLE:
            return list(_PINYIN_TABLE[s])[:12]
        # 从长到短找最长有效前缀
        for n in range(len(s) - 1, 0, -1):
            pre = s[:n]
            if pre in _PINYIN_TABLE:
                return list(_PINYIN_TABLE[pre])[:12]
        # 没有完整音节，做前缀扩展（打了一半的情况）
        out = []
        for k, v in _PINYIN_TABLE.items():
            if k.startswith(s):
                out.extend(list(v)[:2])
            if len(out) >= 12:
                break
        return out[:12]

    def _consumed_len(self) -> int:
        """当前拼音串里，被候选消耗掉的字母数。

        选中候选后要把这段字母从 pinyin 里去掉；
        多打的字母留着继续当作下一段拼音。
        """
        s = self.pinyin
        if s in _PINYIN_TABLE:
            return len(s)
        for n in range(len(s) - 1, 0, -1):
            if s[:n] in _PINYIN_TABLE:
                return n
        return 0

    def refresh_hint(self):
        # 性能：不再每次销毁重建候选按钮。
        # 旧实现每敲一个字母就 remove 全部子控件 + new Button + 连信号，
        # 实测连打 6 个字母要 354ms，手感明显发滞。
        # 改为预建固定数量的按钮池，只改 label 和显隐。
        #
        # 再加一层缓存：候选字符串没变就不动 UI。实测 GTK 每次改 label
        # 都会触发布局重排（单次 ~75ms），而打拼音时很多字母（如 n→nh）
        # 候选完全一样，白刷一遍就是白卡一次。
        # 元组用 hint+cands 做 key：拼音变了但候选没变也要更新 hint 文字，
        # 所以三者一起比。
        key = (self.cn_mode, self.pinyin, tuple(self._candidates()))
        if key == getattr(self, "_hint_key", None):
            return
        self._hint_key = key

        CAND_POOL = 12
        kids = self.cand_box.get_children()
        if not kids:
            for _ in range(CAND_POOL):
                b = Gtk.Button(label="")
                b.set_relief(Gtk.ReliefStyle.NONE)
                b.get_style_context().add_class("vkbd-cand")
                b.set_can_focus(False)
                b.connect("clicked", self._on_cand_clicked)
                self.cand_box.pack_start(b, False, False, 0)
            kids = self.cand_box.get_children()
            self.cand_box.show_all()

        if not self.cn_mode:
            self.hint.set_text("英文 · 点「中文」切换拼音输入")
            self.cand_box.hide()
            return

        if not self.pinyin:
            self.hint.set_text("中文拼音 · 输入拼音后选字")
            self.cand_box.hide()
            return

        cands = self._candidates()
        self.hint.set_text("拼音: %s" % self.pinyin)
        if not cands:
            self.cand_box.hide()
            return
        for i, b in enumerate(kids):
            if i < len(cands):
                b.set_label(cands[i])
                b.show()
            else:
                b.hide()
        self.cand_box.show()

    def _on_cand_clicked(self, btn):
        """候选按钮池的共用回调：取当前 label 作为选中字。"""
        ch = btn.get_label()
        if ch:
            self._pick(ch)
        return False

    def _pick(self, ch: str):
        self.on_text(ch)
        used = self._consumed_len()
        # 只去掉被这次选字消耗的字母，剩下的继续当下一段拼音
        self.pinyin = self.pinyin[used:] if used else ""
        self.refresh_hint()

    def _commit_pinyin(self):
        """拼音还没选字时，把候选第一个上屏（没候选就把拼音当英文上屏）。"""
        cands = self._candidates()
        if cands:
            self._pick(cands[0])
        elif self.pinyin:
            self.on_text(self.pinyin)
            self.pinyin = ""
            self.refresh_hint()

    def _toggle_cn(self):
        self.cn_mode = not self.cn_mode
        self.pinyin = ""
        self._fill_keys()

    def _enter(self):
        self.on_enter()

    def _hide(self):
        self.on_hide()

    # ── 对外 ────────────────────────────────────────────────────
    def fit_to_height(self, avail_h: int):
        """按可用高度决定是否用紧凑布局。

        历史坑：旧逻辑是 avail_h < 950 就切紧凑，导致 1280x800 的屏
        （avail_h=800）永远命中紧凑档，按键只有 29px 高，触摸根本点不准，
        反而还白占 84px 对话区。

        现在改成真实高度预算：标准键盘约 246px（5 行 × 44px 键 + 间距），
        顶栏 58 + 输入栏 73 + 键盘 246 = 377px，留 800-377 = 423px 给对话区，
        完全够用。只有“窗口高 < 键盘 + 至少要给对话区的 260px”才该紧凑。
        """
        KBD_STD_H = 260      # 标准（也是修正后压缩）键盘实测高度
        MIN_CHAT_H = 260     # 对话区保底高度
        need_compact = avail_h < (KBD_STD_H + MIN_CHAT_H + 130)
        if need_compact != self.compact:
            self.compact = need_compact
            self._fill_keys()

    def show(self):
        # 容器被调用方设了 no_show_all，子孙不会自动跟随，
        # 所以这里要显式 show_all 一次（容器自己不显示，靠外部 show）。
        self.widget.show_all()
        self.widget.show()
        self.cand_box.hide()
        self.refresh_hint()

    def hide(self):
        self.widget.hide()

    @property
    def visible(self) -> bool:
        return self.widget.get_visible()
