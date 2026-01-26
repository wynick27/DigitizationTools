# 文本化工具


## OCR后文本校对工具

<img width="1602" height="932" alt="image" src="https://github.com/user-attachments/assets/0b1ee93b-8242-4ca9-9638-223bd3cb49ec" />

用来校对分页的文本，自动进行文本比较和对齐，可以加载PaddleOCR数据，  
使用方法：

新建config.json文件，填入配置信息保存
示例

```json
{
    "pdf_path": "拉汉科技词典.pdf",
    "image_dir": "images",
    "start_page": 1,
    "end_page": 799,
    "page_offset": 10,
    "text_path_left": "拉汉科技词典_gemini.txt",
    "text_path_right": "拉汉科技词典_quark.txt",
    "ocr_json_path": "ocr_results",
    "regex_left": "^\\*\\*(.*?)\\*\\*",
    "regex_right": "^([a-zA-Z]+)",
    "ocr_api_url": "",
    "ocr_api_token": ""
}
```
pdf_path和image_dir选择一个填入即可  
start_page：起始页码  
end_page：结束页码  
page_offset：页码便宜，就是第一页对应pdf中的多少页  
text_path_left，text_path_right：左右的文本，分页符为<页码>  
ocr_json_path：用于存放paddleocr返回的json，json文件名需要是page_页码.json的格式  
regex_left，regex_right左右两侧的词头正则表达式，选填，用于高亮词头  
ocr_api_ur，ocr_api_token：paddleocr的api地址和token，可以在[官网申请](https://aistudio.baidu.com/paddleocr)  
